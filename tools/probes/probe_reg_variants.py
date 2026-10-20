"""注册 429 变体测试：同一时刻用不同 payload 变体打 register/byEmail，
判断 429 卡在哪个维度（clientId / source / 接口本身）。

直连（不走代理）排除代理因素。每个变体一次 browser_post。
"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, time
sys.path.insert(0, "/app/working/intern-register")

from src.waf_bypass import WafSolver
from src.crypto_rsa import encrypt_password

GW = "https://sso.openxlab.org.cn/gw/uaa-be/api/v1"
CID = "dagw07mkg1bazlxzoy31"


def mk(i, **over):
    em = f"zqxv{5000 + i}@example.com"
    p = {"username": f"zqxv{5000 + i}", "email": em,
         "password": encrypt_password(em, "Aa11bb22!"),
         "source": "discovery", "clientId": CID}
    p.update(over)
    return p


VARIANTS = [
    ("原样(discovery+CID)", {}),
    ("source=web", {"source": "web"}),
    ("无 source", {"source": ""}),
    ("无 clientId", {"clientId": ""}),
    ("source=openxlab", {"source": "openxlab"}),
]

for i, (name, over) in enumerate(VARIANTS, 1):
    t0 = time.time()
    try:
        res = WafSolver.get().browser_post(f"{GW}/register/byEmail", mk(i, **over),
                                           proxy=None)
        tx = str(res.get("text") or "")
        st = res.get("status")
        tag = "429限流" if (st == 429 or "too many request" in tx.lower()) else "非429!"
        print(f"[{tag}] {name}: status={st} text={tx[:110]!r} "
              f"({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"[异常] {name}: {type(e).__name__}: {str(e)[:100]} "
              f"({round(time.time()-t0,1)}s)", flush=True)
    time.sleep(3)

try:
    WafSolver.get().shutdown()
except Exception:
    pass
