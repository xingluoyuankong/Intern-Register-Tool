"""429 维度区分（走代理，不直连）。

变体测试（直连）已证：无 source → 200。现在走代理仍 429。
本测试用**代理**再跑几个变体，区分：
  - 代理 + 无 source + 无 clientId → 若 200：是 clientId/活动维度
  - 代理 + 无 source + 无 clientId → 若 429：是出口 IP 维度（配额被烧）
"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, time
sys.path.insert(0, "/app/working/intern-register")

from src.waf_bypass import WafSolver
from src.crypto_rsa import encrypt_password

GW = "https://sso.openxlab.org.cn/gw/uaa-be/api/v1"
SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]


def mk(i, **over):
    em = f"zqxy{6000 + i}@example.com"
    p = {"username": f"zqxy{6000 + i}", "email": em,
         "password": encrypt_password(em, "Aa11bb22!")}
    p.update(over)
    return p


CASES = [
    ("代理 无source 无clientId", 1, {"source": "", "clientId": ""}),
    ("代理 无source 有clientId", 2, {"source": "",
                                 "clientId": "dagw07mkg1bazlxzoy31"}),
    ("代理 无source 无clientId(另一槽)", 5, {"source": "", "clientId": ""}),
]
for name, si, over in CASES:
    t0 = time.time()
    try:
        res = WafSolver.get().browser_post(f"{GW}/register/byEmail", mk(si, **over),
                                           proxy=slots[si - 1])
        tx = str(res.get("text") or "")
        st = res.get("status")
        tag = "429" if (st == 429 or "too many" in tx.lower()) else "非429!"
        print(f"[{tag}] {name}: status={st} text={tx[:100]!r} "
              f"({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"[异常] {name}: {type(e).__name__}: {str(e)[:90]} "
              f"({round(time.time()-t0,1)}s)", flush=True)
    time.sleep(3)

try:
    WafSolver.get().shutdown()
except Exception:
    pass
