"""服务器端：测 WafSolver.browser_post 能否过 WAF 注册。

用法：.venv/bin/python -u tools/probes/probe_browser_reg.py
"""
import sys, os, time
sys.path.insert(0, "/app/working/intern-register")
os.environ.setdefault("IR_HEADFUL", "0")
from src.waf_bypass import WafSolver

SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]
print("槽位数:", len(slots), flush=True)

payload = {"username": "zqxbpreg1", "email": "zqxbpreg1@example.com", "password": "aa11",
           "source": "discovery", "clientId": "dagw07mkg1bazlxzoy31"}
for idx, s in enumerate(slots[:3], 1):
    t0 = time.time()
    try:
        res = WafSolver.get().browser_post("/register/byEmail", payload, proxy=s)
        print(f"slot{idx}: status={res.get('status')} text={str(res.get('text'))[:150]!r} ({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"slot{idx}: FAIL {type(e).__name__}: {str(e)[:150]} ({round(time.time()-t0,1)}s)", flush=True)
try:
    WafSolver.get().shutdown()
except Exception:
    pass
