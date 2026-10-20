# byAccount A0101 参数校验排错：T001 过后用同一 cvp 重放变体矩阵，看哪个形状过。
# 结构：跑生产 attempt（T001 场景会触发内置自包 byAccount 并留下 cvp），
#       尝试结束后若手上有 cvp，就在新页面里重放 6 种 body/header 变体。
import json
import sys
import time

sys.path.insert(0, ".")

from src import config
from src.browser.launch import launch_browser, current_engine
from src.browser.attempt import _run_attempt
from src.browser.session import _retry_loop
from src.pipeline import login_identifier
from src.proxypool import slot_alive
from src.crypto_rsa import encrypt_password


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
log(f"slot: {'OK' if slot else 'NONE'}")

EMAIL = "ACCOUNT@EXAMPLE.COM"
import glob as _glob


def _find_pwd(email):
    """latest.json 只是最新一轮快照，老账号要去历史/全量里找。"""
    files = (["ledger/latest.json"]
             + sorted(_glob.glob("ledger/runs/*/results-*.json"),
                      reverse=True)
             + ["ledger/export_all.json"])
    for f in files:
        try:
            dd = json.load(open(f))
        except Exception:
            continue
        rr = dd if isinstance(dd, list) else dd.get("records") or []
        for x in rr:
            if (x.get("email") or "").lower() == email:
                p = x.get("password") or x.get("pwd")
                if p:
                    return p
    return None


PWD = _find_pwd(EMAIL)
assert PWD, "账号密码没找到"
IDENT = login_identifier(EMAIL, "")
ENC = encrypt_password(IDENT, PWD)


def run_once(tag):
    with launch_browser(slot, headless=True) as browser:
        return _run_attempt(
            browser, account=IDENT, password=PWD, headless=True,
            timeout=150, screenshot_prefix="/tmp/probe_ba",
            verbose=True, tag=tag, proxy=None,
            fingerprint_browser=(current_engine() == "camoufox"))


res = _retry_loop(run_once, attempts=1, cooldown=0, verbose=True,
                  retry_hint="")
cs = res.captcha_stage or {}
cvp, sid = cs.get("cvp", ""), cs.get("scene_id", "")
log(f"attempt ok={res.ok} cvp={'YES' if cvp else 'no'} "
    f"verify={cs.get('verify')}")
if not cvp:
    print("VERDICT: NO_CVP_THIS_ROUND")
    raise SystemExit

with launch_browser(slot, headless=True) as browser:
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto("https://sso.openxlab.org.cn/login",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)

    JS = """async (a) => {
        const headers = {'Content-Type': 'application/json', 'lang': 'zh-CN'};
        const body = Object.assign({}, a.body);
        if (a.mode.indexOf('h') >= 0) {
            headers['captchaVerifyParam'] = a.cvp;
            headers['sceneId'] = a.sceneId;
            headers['prefix'] = 'lvtb1n';
        }
        if (a.mode.indexOf('b') >= 0) {
            body.captchaVerifyParam = a.cvp;
            body.sceneId = a.sceneId;
            body.prefix = 'lvtb1n';
        }
        const r = await fetch('/gw/uaa-be/api/v1/login/byAccount', {
            method: 'POST', credentials: 'include',
            headers: headers, body: JSON.stringify(body)});
        return {status: r.status, text: await r.text()};
    }"""

    base = {"account": IDENT, "password": ENC}
    variants = [
        ("v1-h-t1str", "h", dict(base, type="1", marketing="")),
        ("v2-h-t1int", "h", dict(base, type=1, marketing="")),
        ("v3-b-t1str", "b", dict(base, type="1", marketing="")),
        ("v4-hb-t1str", "hb", dict(base, type="1", marketing="")),
        ("v5-h-notype", "h", dict(base, marketing="")),
        ("v6-h-nomkt", "h", dict(base, type="1")),
    ]
    hit = ""
    for name, mode, body in variants:
        try:
            r = page.evaluate(JS, {"cvp": cvp, "sceneId": sid,
                                   "mode": mode, "body": body})
            txt = (r.get("text") or "")[:130]
            log(f"{name}: {r.get('status')} {txt}")
            if '"msgCode":"10000"' in txt:
                hit = name
                break
        except Exception as e:
            log(f"{name}: ERR {type(e).__name__} {str(e)[:80]}")
        time.sleep(2)
    print(f"VERDICT: {hit + '_PASSED' if hit else 'ALL_A0101'}")
