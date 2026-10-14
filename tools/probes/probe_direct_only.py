"""判定 429 是「按出口 IP」还是「站点全局限流」：**完全不走代理**打一次
register/byEmail（浏览器路径，已能过 WAF）。

- 直连也 429  ⇒ 全局限流，换出口没用，只能放慢节奏 / 换活动。
- 直连成功    ⇒ 限流按 IP 记，代理池的 IP 被烧，需换出口或加大重试。
"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, time
sys.path.insert(0, "/app/working/intern-register")

from src.waf_bypass import WafSolver
from src.crypto_rsa import encrypt_password

GW = "https://sso.openxlab.org.cn/gw/uaa-be/api/v1"


def mk(i):
    em = f"zqxd{8000 + i}@example.com"
    return {"username": f"zqxd{8000 + i}", "email": em,
            "password": encrypt_password(em, "Aa11bb22!"),
            "source": "discovery", "clientId": "dagw07mkg1bazlxzoy31"}


for i in range(2):          # 打两次，看是否稳定
    t0 = time.time()
    try:
        res = WafSolver.get().browser_post(f"{GW}/register/byEmail", mk(i), proxy=None)
        tx = str(res.get("text") or "")
        print(f"DIRECT#{i+1}: status={res.get('status')} text={tx[:160]!r} "
              f"({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"DIRECT#{i+1}: FAIL {type(e).__name__}: {str(e)[:140]} "
              f"({round(time.time()-t0,1)}s)", flush=True)
    time.sleep(5)

try:
    WafSolver.get().shutdown()
except Exception:
    pass
