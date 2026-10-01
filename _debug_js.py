"""调试：把挑战页脚本交给 node 直接跑，打印真实异常与 document.cookie。"""
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")
from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402
from src.waf_js import _scripts  # noqa: E402

SLOT = ("http://Link.rl2.U3lzZmdnX25VdHFVQWdudFv5et_1t6OX:"
        "@resin-proxy.xzxyuan.ccwu.cc:2268")
sso = SSOClient(proxy=SLOT)
tag = str(int(time.time()))[-6:]
email = f"dbg{tag}@example.invalid"
payload = {
    "username": f"db{tag}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
r = sso.session.post(f"{config.SSO_GW}/register/byEmail",
                     headers=sso._headers("/register"), json=payload,
                     timeout=30)
html = r.text
print("challenged:", "acw_sc__v2" in html, "| len", len(html), flush=True)

code = _scripts(html)
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
    f.write(code)
    path = f.name
out = subprocess.run(["node", path], capture_output=True, text=True,
                     timeout=40, encoding="utf-8", errors="replace")
print("node exit:", out.returncode, flush=True)
print("node stdout:", (out.stdout or "").strip()[:300], flush=True)
print("node stderr:", (out.stderr or "").strip()[:800], flush=True)
Path(path).unlink(missing_ok=True)
