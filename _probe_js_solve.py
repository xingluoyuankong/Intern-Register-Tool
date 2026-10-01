"""服务器端到端：走 Resin 槽位 → 挑战 → JS 引擎算 cookie → 重放注册。"""
import os
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402
from src.waf_js import solve_acw  # noqa: E402

SLOT = ("http://Link.rl2.dmpZa2t2SENyUlVjRFZ3WZieXZLTMML2:"
        "@resin-proxy.xzxyuan.ccwu.cc:2268")
REAL = "--real" in sys.argv


def log(*a):
    print(f"[{time.time() % 10000:07.2f}]", *a, flush=True)


sso = SSOClient(proxy=SLOT)
tag = str(int(time.time()))[-6:]
dom = "@hotmail.com" if REAL else "@example.invalid"
email = f"js{tag}{dom}"
payload = {
    "username": f"js{tag}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
H = sso._headers("/register")
URL = f"{config.SSO_GW}/register/byEmail"

sso.prime_session()
log("primed, cookies:", sorted(c.name for c in sso.session.cookies))

r = sso.session.post(URL, headers=H, json=payload, timeout=30)
if "acw_sc__v2" not in r.text:
    log("first POST not challenged:", r.status_code, r.text[:120])
    sys.exit(0)

log("challenged; solving with JS engine...")
t0 = time.time()
val = solve_acw(r.text)
log(f"solved in {time.time() - t0:.2f}s -> {val[:40]} (len {len(val)})")
if not val:
    log("JS solve FAILED")
    sys.exit(1)

sso.session.cookies.set("acw_sc__v2", val,
                        domain="sso.openxlab.org.cn", path="/")
r2 = sso.session.post(URL, headers=H, json=payload, timeout=30)
ch = "CHALLENGE" if "acw_sc__v2" in r2.text else "pass"
log(f"replay -> {r2.status_code} {ch} | {r2.text[:200]}")
