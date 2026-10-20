# 决定性探针 v2：真实页面 UI 全流程注册，回答「站方为何停发激活邮件」。
# v2：429（IP 维度限流）→ 自动换槽位重试；响应体在页面关闭前读出。

import json
import os
import sys
import time

sys.path.insert(0, "/app/working/intern-register")
os.chdir("/app/working/intern-register")

from src.pipeline import gen_password, gen_username          # noqa: E402
from src.browser.session import BrowserSession               # noqa: E402
from src.proxypool import slot_alive                         # noqa: E402
from src.tempmailio import TempMailIoClient                  # noqa: E402

SLOTS_FILE = ".workbuddy-ai/proxypool/slots_active.txt"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def find_live_slots(max_try=15):
    try:
        slots = [l.strip() for l in open(SLOTS_FILE) if l.strip()]
    except OSError:
        return []
    return [s for s in slots[:max_try] if slot_alive(s, timeout=10)]


def try_register(proxy, addr, u, p, tag):
    """一次完整 UI 注册尝试。返回 [{url,status,ct,body}]；结构异常返回 None。"""
    resp_holder = []
    result = []
    with BrowserSession(headless=True, proxy=proxy) as sess:
        b = sess._browser
        ctx = b.new_context()
        page = ctx.new_page()

        def on_resp(r):
            if "/api/v1/register/" in r.url or "username/check" in r.url:
                try:
                    ct = (r.headers or {}).get("content-type", "")
                    resp_holder.append((r.url, r.status, ct, r))
                    log(f"RESP {r.status} …{r.url[-42:]}")
                except Exception as e:
                    log(f"resp err {e}")

        def on_req(r):
            if "byEmail" in r.url:
                try:
                    log(f"REQ-POST body={(r.post_data or '')[:400]}")
                except Exception:
                    pass

        page.on("response", on_resp)
        page.on("request", on_req)

        log(f"[{tag}] goto /register …")
        page.goto("https://sso.openxlab.org.cn/register",
                  timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        page.screenshot(path=f"/tmp/probe_reg_{tag}_1.png")

        inputs = page.locator("form.register-form input:visible")
        n = inputs.count()
        log(f"[{tag}] register-form inputs={n}")
        if n < 4:
            page.screenshot(path=f"/tmp/probe_reg_{tag}_fail.png")
            return None

        def type_into(idx, text, desc):
            el = inputs.nth(idx)
            el.click()
            page.wait_for_timeout(300)
            for ch in text:
                page.keyboard.type(ch)
                page.wait_for_timeout(55)
            page.wait_for_timeout(400)
            log(f"[{tag}] typed {desc}")

        type_into(0, u, "username")   # 触发用户名可用性校验（真实流的一部分）
        type_into(1, addr, "email")
        type_into(2, p, "password")
        type_into(3, p, "confirm")

        try:
            cb = page.locator(".ant-checkbox-input").first
            if cb.count() and cb.is_visible() and not cb.is_checked():
                cb.click()
                log(f"[{tag}] agreement checkbox ✓")
        except Exception as e:
            log(f"[{tag}] checkbox err {e}")

        btn = page.locator("form.register-form button:visible").last
        btn.click()
        log(f"[{tag}] clicked submit, waiting response …")

        for _ in range(35):
            page.wait_for_timeout(1000)
            if any("byEmail" in x[0] for x in resp_holder):
                break

        # 同意弹窗兜底（W 未置时点提交会弹协议框）
        try:
            modal = page.locator(".ant-modal button:visible")
            if modal.count():
                for i in range(modal.count()):
                    log(f"modal btn[{i}]={modal.nth(i).inner_text()!r}")
                for i in range(modal.count()):
                    t = modal.nth(i).inner_text()
                    if ("同意" in t) or ("Agree" in t):
                        modal.nth(i).click()
                        log(f"[{tag}] clicked modal agree …")
                        for _ in range(35):
                            page.wait_for_timeout(1000)
                            if any("byEmail" in x[0] for x in resp_holder):
                                break
                        break
        except Exception:
            pass

        page.screenshot(path=f"/tmp/probe_reg_{tag}_3.png")

        # 🔴 必须在 ctx 关闭前把 body 读出来（关了就读不到了）
        for url2, st, ct2, r2 in resp_holder:
            try:
                body2 = r2.text()
            except Exception:
                body2 = "<unreadable>"
            if "byEmail" in url2 or "register/active" in url2:
                result.append({"url": url2, "status": st, "ct": ct2,
                               "body": body2[:400]})
                log(f"[{tag}] byEmail -> {st} {body2[:200]}")
    return result


def main():
    mail = TempMailIoClient()
    addr = mail.create_mailbox()[0]
    u, p = gen_username(), gen_password()
    log(f"mailbox={addr}")
    log(f"username={u} pwlen={len(p)}")

    slots = find_live_slots()
    log(f"live slots={len(slots)}")

    final = []
    got_200 = False
    for i, slot in enumerate((slots + [None])[:5]):   # 最多 5 次：4 槽位 + 直连
        tag = f"a{i}"
        try:
            r = try_register(slot, addr, u, p, tag)
        except Exception as e:
            log(f"[{tag}] attempt failed: {type(e).__name__} {str(e)[:120]}")
            continue
        if r is None:
            log(f"[{tag}] page structure unexpected → next")
            continue
        final = r
        be = [x for x in r if "byEmail" in x["url"]]
        if be and be[0]["status"] == 200:
            got_200 = True
            break
        log(f"[{tag}] byEmail status={be[0]['status'] if be else '?'} → 换下一个出口")
        time.sleep(5)

    print("REGISTER_RESULT:", json.dumps(final, ensure_ascii=False)[:800])
    if not got_200:
        print("VERDICT: NO_BYEMAIL_200")
        return

    log("─── 注册成功，轮询激活邮件 240s ───")
    m = mail.wait_for_mail(addr, timeout=240, interval=5)
    if m:
        log(f"MAIL! from={m.from_address} subj={m.subject}")
        lk = m.find_link("active", "activat", "verif", "confirm")
        log(f"link={lk}")
        print("VERDICT: MAIL_OK")
    else:
        log(f"no mail (polls={mail.last_polls} err={mail.last_error})")
        print("VERDICT: MAIL_NONE")


main()
