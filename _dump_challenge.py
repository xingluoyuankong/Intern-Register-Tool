"""dump 代理路径拿到的真实挑战页 HTML（含 JS），落盘供逆向。"""
import re
import sys
import time

sys.path.insert(0, ".")
from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

SLOT = ("http://Link.rl2.U3lzZmdnX25VdHFVQWdudFv5et_1t6OX:"
        "@resin-proxy.xzxyuan.ccwu.cc:2268")
sso = SSOClient(proxy=SLOT)
tag = str(int(time.time()))[-6:]
email = f"dmp{tag}@example.invalid"
payload = {
    "username": f"dm{tag}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
r = sso.session.post(f"{config.SSO_GW}/register/byEmail",
                     headers=sso._headers("/register"), json=payload,
                     timeout=25)
html = r.text
with open("_challenge_dump.html", "w", encoding="utf-8") as f:
    f.write(html)
print("saved", len(html), "bytes -> _challenge_dump.html", flush=True)
for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S):
    body = m.group(1).strip()
    if not body:
        continue
    print("=" * 20, "script block", len(body), "chars", "=" * 20, flush=True)
    print(body[:2000], flush=True)
