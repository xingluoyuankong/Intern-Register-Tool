# 生产级登录 + 全程抓包：验证码过了但 byAccount/JWT 不来，看服务端回了什么。
# 复用生产 8 步流程（_run_attempt），临时挂 on_response 抓 login/captcha 响应。
import json
import sys
import time

sys.path.insert(0, ".")

from src import config
from src.browser.launch import launch_browser, current_engine
from src.browser.attempt import _run_attempt
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
log(f"slot: {'OK' if slot else 'NONE'}")

EMAIL = __import__("sys").argv[1] if len(__import__("sys").argv) > 1 else "ACCOUNT@EXAMPLE.COM"
import glob as _glob


def _find_pwd(email):
    for f in (["ledger/latest.json"]
              + sorted(_glob.glob("ledger/runs/*/results-*.json"),
                       reverse=True)
              + ["ledger/export_all.json"]):
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

with launch_browser(slot, headless=True) as browser:
    # 给 _run_attempt 挂抓包：直接 monkeypage —— 简化：自己建 context 后传 browser？
    # _run_attempt 自己 new_context，抓包挂不到。改用 attempt 内部状态打点
    # 不够 —— 这里退一步：跑生产 attempt（完整逻辑），结束后检查结果；
    # 抓包改为挂 browser 级（context 是内部建的）→ 不可行。
    # ⇒ 直接跑生产 attempt + 开 verbose，吃它的事件流；
    #   另在进程内打 login/byAccount 响应做不到，就靠 st.ev + reason。
    res = _run_attempt(browser, account=IDENT, password=PWD,
                       headless=True, timeout=150,
                       screenshot_prefix="/tmp/probe_login_run",
                       verbose=True, tag="probe",
                       proxy=None,
                       fingerprint_browser=(current_engine() == "camoufox"))
    log(f"ok={res.ok} reason={res.reason} jwt={'YES' if res.jwt else 'no'}")
    log(f"captcha={res.captcha_stage}")
    log(f"timings={getattr(res, 'timings', None)}")
