"""服务器端：带 XDG_CACHE_HOME 测浏览器解法（与 regloop 同环境）。

用法：XDG_CACHE_HOME=/app/working/intern-register/.cache .venv/bin/python -u tools/probes/probe_browser_env.py
"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, time
sys.path.insert(0, "/app/working/intern-register")
from src.waf_bypass import WafSolver

print("XDG_CACHE_HOME =", os.environ.get("XDG_CACHE_HOME"), flush=True)
SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]
payload = {"username": "zqxbpenv1", "email": "zqxbpenv1@example.com", "password": "aa11",
           "source": "discovery", "clientId": "dagw07mkg1bazlxzoy31"}
for idx, s in enumerate(slots[:3], 1):
    t0 = time.time()
    try:
        res = WafSolver.get().browser_post("/register/byEmail", payload, proxy=s)
        print(f"slot{idx}: status={res.get('status')} text={str(res.get('text'))[:130]!r} ({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"slot{idx}: FAIL {type(e).__name__}: {str(e)[:130]} ({round(time.time()-t0,1)}s)", flush=True)
try:
    WafSolver.get().shutdown()
except Exception:
    pass
