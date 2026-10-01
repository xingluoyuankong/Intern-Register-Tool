"""协议终验：GET /register 种会话（ssxmod 行为链）→ POST register（算法 cookie）。"""
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

sso = SSOClient()

# 1) 先 GET 注册页 —— 种下 acw_tc + ssxmod_itna/itna2 行为链
r0 = sso.session.get(f"{config.SSO_BASE}/register",
                     headers=sso._headers("/register"), timeout=30)
print("[0] GET /register:", r0.status_code,
      "challenge" if "acw_sc__v2" in r0.text else "normal",
      "| cookies:", sorted(c.name for c in sso.session.cookies), flush=True)

# 2) 若首 GET 就被挑战 → 算法解掉再 GET 一次
if "acw_sc__v2" in r0.text:
    import re as _re
    m = _re.search(r"arg1='([0-9A-Fa-f]{40})'", r0.text)
    if m:
        sso._solve_waf(r0)
        r0 = sso.session.get(f"{config.SSO_BASE}/register",
                             headers=sso._headers("/register"), timeout=30)
        print("[0b] re-GET:", r0.status_code,
              "challenge" if "acw_sc__v2" in r0.text else "normal", flush=True)

# 3) POST 注册（非法邮箱，不耗配额；挑战由 _post 内部算法兜）
tag = str(int(time.time()))[-8:]
email = f"finaltest{tag}@hotmail.com"
payload = {
    "username": f"ft{tag[-6:]}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
r = sso._post("/register/byEmail", payload)
print("[1] register:", r.status_code, r.text[:200], flush=True)
