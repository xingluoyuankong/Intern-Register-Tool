"""判断 429 是「出口池被烧(按 IP)」还是「全局限流」：同一 payload 分别
用 直连 / 各槽位 打一次 register/byEmail，对比返回。

浏览器路径（browser_post）已能过 WAF，所以这里直接用它，隔离掉 WAF 变量。
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
    email = f"zqx429{i}@example.com"
    return {
        "username": f"zqx429{i}",
        "email": email,
        "password": encrypt_password(email, "Aa11bb22!"),
        "source": "discovery",
        "clientId": "dagw07mkg1bazlxzoy31",
    }


# 1) 直连（不走任何代理）
for tag, px in [("DIRECT", None)]:
    t0 = time.time()
    try:
        res = WafSolver.get().browser_post(f"{GW}/register/byEmail", mk(0), proxy=px)
        print(f"{tag}: status={res.get('status')} "
              f"text={str(res.get('text'))[:150]!r} ({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"{tag}: FAIL {type(e).__name__}: {str(e)[:150]} ({round(time.time()-t0,1)}s)", flush=True)

# 2) 逐个槽位
SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
try:
    slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]
except Exception:
    slots = []
for idx, s in enumerate(slots[:5], 1):
    t0 = time.time()
    try:
        res = WafSolver.get().browser_post(f"{GW}/register/byEmail", mk(idx), proxy=s)
        print(f"slot{idx}: status={res.get('status')} "
              f"text={str(res.get('text'))[:150]!r} ({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"slot{idx}: FAIL {type(e).__name__}: {str(e)[:150]} ({round(time.time()-t0,1)}s)", flush=True)

try:
    WafSolver.get().shutdown()
except Exception:
    pass
