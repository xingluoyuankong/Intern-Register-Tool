"""定点诊断：本机走槽位注册，逐步打印三种解法的结果与异常。"""
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402
from src.waf_bypass import find_headless_browser  # noqa: E402

SLOT = ("http://Link.rl2.U3lzZmdnX25VdHFVQWdudFv5et_1t6OX:"
        "@resin-proxy.xzxyuan.ccwu.cc:2268")


def log(*a):
    print(f"[{time.time() % 10000:07.2f}]", *a, flush=True)


log("headless browser:", find_headless_browser()[:80])
sso = SSOClient(proxy=SLOT)
tag = str(int(time.time()))[-6:]
email = f"diag{tag}@example.invalid"
payload = {
    "username": f"dg{tag}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
H = sso._headers("/register")
URL = f"{config.SSO_GW}/register/byEmail"

sso.prime_session()
r = sso.session.post(URL, headers=H, json=payload, timeout=30)
log("first POST:", r.status_code, "challenge" if "acw_sc__v2" in r.text
    else "pass")

for method in ("js", "browser", "algo"):
    t0 = time.time()
    try:
        sso._solve_waf(r, method=method)
        names = sorted(c.name for c in sso.session.cookies)
        log(f"{method}: OK in {time.time() - t0:.1f}s | cookies={names}")
    except Exception as ex:  # noqa: BLE001
        log(f"{method}: FAIL in {time.time() - t0:.1f}s "
            f"{type(ex).__name__}: {str(ex)[:150]}")
    r2 = sso.session.post(URL, headers=H, json=payload, timeout=30)
    ch = "CHALLENGE" if "acw_sc__v2" in r2.text else "pass"
    log(f"  replay -> {r2.status_code} {ch} | {r2.text[:80]}")
    if ch == "pass":
        break
    r = r2
