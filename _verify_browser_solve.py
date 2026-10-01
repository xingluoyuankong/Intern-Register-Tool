"""定点验证：浏览器求解 cookie（waf_bypass.solve）→ requests 重放注册。

这条路径就是昨晚 10/10 的原路径。此前被 _solve_waf 里"旧算法假成功"堵死
（算出废 cookie 就 return，浏览器永不执行）。现在 sso 已改轮换，本脚本
手动验证浏览器解出的 cookie 能否让 requests 重放通过。
"""
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402
from src.waf_bypass import WafSolver  # noqa: E402

SLOT = ("http://Link.rl2.WWloM2xSbTlNOTJGV3BhWPSs4a2cf3Wd:"
        "@resin-proxy.xzxyuan.ccwu.cc:2268")


def log(*a):
    print(f"[{time.time() % 10000:07.2f}]", *a, flush=True)


sso = SSOClient(proxy=SLOT)
tag = str(int(time.time()))[-6:]
email = f"bs{tag}@example.invalid"
payload = {
    "username": f"bs{tag}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
H = sso._headers("/register")
URL = f"{config.SSO_GW}/register/byEmail"

sso.prime_session()
r = sso.session.post(URL, headers=H, json=payload, timeout=30)
log("first POST:", r.status_code,
    "challenge" if "acw_sc__v2" in r.text else "pass")
if "acw_sc__v2" not in r.text:
    log("no challenge:", r.text[:120])
    sys.exit(0)

t0 = time.time()
try:
    cookies = WafSolver.get().solve(r.text)
    log(f"browser solve {time.time() - t0:.1f}s -> {cookies}")
except Exception as ex:  # noqa: BLE001
    log(f"browser solve EXC {type(ex).__name__}: {str(ex)[:200]}")
    sys.exit(1)

for k, v in cookies.items():
    sso.session.cookies.set(k, v, domain="sso.openxlab.org.cn", path="/")
r2 = sso.session.post(URL, headers=H, json=payload, timeout=30)
ch = "CHALLENGE" if "acw_sc__v2" in r2.text else "pass"
log(f"replay -> {r2.status_code} {ch} | {r2.text[:150]}")
