"""查 register / activate 的原始响应里有没有可直接用的 token（绕开浏览器登录）。

背景：登录建 key 卡死 —— 走代理浏览器加载不动，直连 IP 已 405。
若 activate 响应直接带 jwt/token，就能纯协议建 key，彻底绕开浏览器。
"""
import sys
import time

sys.path.insert(0, ".")
from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

SLOT = sys.argv[1] if len(sys.argv) > 1 else None
print("slot:", (SLOT or "直连")[:60])

sso = SSOClient(proxy=SLOT, timeout=25)
tag = str(int(time.time()))[-7:]
email = f"probe{tag}@outlook.com"
username = f"pz{tag[-6:]}"

sso.prime_session()
r1 = sso._post("/register/byEmail", {
    "username": username, "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE, "clientId": config.CLIENT_ID,
})
print("=== register ===", r1.status_code)
print(r1.text[:400])
try:
    d1 = r1.json()
    print("keys:", list((d1.get("data") or {}).keys()) if isinstance(d1.get("data"), dict) else d1.get("data"))
except Exception as ex:  # noqa: BLE001
    print("not json:", type(ex).__name__)

# activate 需要邮件里的链接；先看 register 成功后能否直接拿到别的东西
# 这里只打印 register 响应，激活链路在 run.py 里已验证 ok
