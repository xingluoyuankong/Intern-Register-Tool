"""服务器端：验证 algo 解法能否穿过注册接口 WAF。

用法（服务器）：.venv/bin/python -u tools/probes/probe_waf_algo.py
"""
import sys, time, re
sys.path.insert(0, "/app/working/intern-register")
from src.sso import SSOClient

SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]
print("槽位数:", len(slots))

passed = 0
for idx, s in enumerate(slots, 1):
    sso = SSOClient(proxy=s)
    sso.abandon_on_challenge = False
    try:
        t0 = time.time()
        r = sso.register(f"zqxsrv{idx}9", f"zqxsrv{idx}9@example.com", "aa11")
        print(f"slot{idx}: ok={r.ok} code={r.msg_code!r} msg={r.msg!r} ({round(time.time()-t0,1)}s)")
        passed += 1
    except Exception as e:
        print(f"slot{idx}: EXC {type(e).__name__}: {str(e)[:90]}")
print("拿到业务响应的槽位数:", passed, "/", len(slots))
