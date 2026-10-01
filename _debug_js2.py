"""调试 JS 求解：抓一份真实挑战页 → node 执行 → 打印真实异常。"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")
from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402
from src.waf_js import _STUB  # noqa: E402

import re  # noqa: E402

SLOT = ("http://Link.rl2.WWloM2xSbTlNOTJGV3BhWPSs4a2cf3Wd:"
        "@resin-proxy.xzxyuan.ccwu.cc:2268")

sso = SSOClient(proxy=SLOT)
tag = str(int(time.time()))[-6:]
email = f"jd{tag}@example.invalid"
payload = {
    "username": f"jd{tag}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
r = sso.session.post(f"{config.SSO_GW}/register/byEmail",
                     headers=sso._headers("/register"), json=payload,
                     timeout=30)
html = r.text
print("challenged:", "acw_sc__v2" in html, "len", len(html), flush=True)
if "acw_sc__v2" not in html:
    print("not challenged:", r.status_code, r.text[:120], flush=True)
    sys.exit(0)

blocks = [m.group(1) for m in
          re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I)]
blocks = [b for b in blocks if b.strip()]
print("script blocks:", [len(b) for b in blocks], flush=True)

body = "\n".join(blocks)
js = (
    _STUB
    + "\nvar __err='none';\ntry {\n"
    + body
    + "\n} catch(e) { __err = (e && e.stack) ? e.stack : String(e); }\n"
    + "require('fs').writeFileSync(__OUT__, "
      "'COOKIE=' + String(document.cookie) + '\\nERR=' + __err);\n"
)
with tempfile.TemporaryDirectory() as d:
    jsf = Path(d) / "ch.cjs"
    out = Path(d) / "out.txt"
    jsf.write_text(js.replace("__OUT__", repr(str(out))), encoding="utf-8")
    p = subprocess.run(["node", str(jsf)], capture_output=True, timeout=40,
                       text=True, encoding="utf-8", errors="replace")
    print("node rc:", p.returncode, flush=True)
    if p.stderr:
        print("node stderr:", p.stderr[:1500], flush=True)
    if out.exists():
        txt = out.read_text(encoding="utf-8")
        print("---- result ----", flush=True)
        print(txt[:1200], flush=True)
    else:
        print("NO OUTPUT FILE (script crashed before write)", flush=True)
