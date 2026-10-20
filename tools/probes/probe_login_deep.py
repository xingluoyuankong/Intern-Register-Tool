# 深挖：验证码已过(ok=True)但 JWT 不来。抓登录 API 响应 + cookie + 页面态。
import json
import sys
import time

sys.path.insert(0, ".")

from src import config
from src.browser.session import BrowserSession
from src.proxypool import slot_alive
from src.pipeline import login_identifier


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


slot = None
t0 = time.time()
while time.time() - t0 < 240 and not slot:
    for s in config.proxy_slots():
        if slot_alive(s, timeout=5.0):
            slot = s
            break
    if not slot:
        time.sleep(45)
log(f"slot: {'OK' if slot else 'NONE(直连,作废)'}")

EMAIL = "ACCOUNT@EXAMPLE.COM"
d = json.load(open("ledger/latest.json"))
recs = d if isinstance(d, list) else d.get("records") or []
PWD = next(x.get("password") for x in recs
           if (x.get("email") or "").lower() == EMAIL)
IDENT = login_identifier(EMAIL, "")

hits = []          # (url, status, body前200)
with BrowserSession(headless=True, proxy=slot) as sess:
    b = sess._browser
    ctx = b.new_context()
    page = ctx.new_page()

    def on_resp(r):
        u = r.url
        if any(k in u for k in ("login", "byAccount", "captcha",
                                "verify", "jwt", "token", "getUserInfo")):
            try:
                body = r.text()[:200]
            except Exception:
                body = "<nf>"
            hits.append((u, r.status, body))
            log(f"RESP {r.status} …{u[-52:]} {body[:90]!r}")

    page.on("response", on_resp)
    log("goto /login")
    page.goto("https://sso.openxlab.org.cn/login",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)

    tab = page.locator(".ant-tabs-tab:visible").first
    if tab.count():
        try:
            tab.click()
            log("clicked 密码登录 tab")
        except Exception:
            pass
        page.wait_for_timeout(800)
    ui = page.locator("form#login-form input#username, form input#username")
    pi = page.locator("form input#password")
    if not ui.count():
        ui = page.locator("input[placeholder*='用户名'], input[placeholder*='邮箱']")
        pi = page.locator("input[type='password']")
    ui.first.fill(IDENT)
    page.wait_for_timeout(400)
    pi.first.fill(PWD)
    page.wait_for_timeout(500)
    log("filled; submit")
    page.locator("form button:visible").last.click()

    for i in range(150):                      # 150s 观察
        page.wait_for_timeout(1000)
        jwt_cookie = [c["name"] for c in ctx.cookies()
                      if "token" in c["name"].lower() or "jwt" in c["name"].lower()]
        if i in (10, 25, 45, 70, 100, 140):
            log(f"t={i}s cookies={[c['name'] for c in ctx.cookies()][:8]} "
                f"jwtish={jwt_cookie}")
            page.screenshot(path=f"/tmp/login_probe_{i}.png")
    log("── 全部命中响应 ──")
    for u, st, bd in hits:
        log(f"{st} …{u[-60:]} {bd[:110]!r}")
