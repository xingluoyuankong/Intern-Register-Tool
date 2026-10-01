"""协议终验 v2：GET 种会话 → POST（挑战则算法解）→ 全程无浏览器。"""
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

sso = SSOClient()
H = sso._headers("/register")


def log(*a):
    print(f"[{time.time() % 10000:07.2f}]", *a, flush=True)


r0 = sso.session.get(f"{config.SSO_BASE}/register", headers=H, timeout=30)
log("GET:", r0.status_code, "challenge" if "acw_sc__v2" in r0.text else "normal",
    "| cookies:", sorted(c.name for c in sso.session.cookies))

tag = str(int(time.time()))[-8:]
email = f"fv2test{tag}@hotmail.com"
payload = {
    "username": f"f2{tag[-6:]}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
URL = f"{config.SSO_GW}/register/byEmail"

for i in range(3):
    r = sso.session.post(URL, headers=H, json=payload, timeout=30)
    if "acw_sc__v2" in r.text:
        log(f"post{i}: challenge -> algo solve")
        sso._solve_waf(r)
        time.sleep(0.5)
        continue
    log(f"post{i}: {r.status_code} {r.text[:200]}")
    break
