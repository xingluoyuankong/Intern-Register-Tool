"""服务器端 A/B：prime_session 前后对注册成功率的影响。

用法：.venv/bin/python -u tools/probes/probe_prime_ab.py
"""
import sys, time
sys.path.insert(0, "/app/working/intern-register")
from src.sso import SSOClient

SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]

def run(prime: bool, idx: int, s: str):
    sso = SSOClient(proxy=s)
    sso.abandon_on_challenge = False
    t0 = time.time()
    try:
        if prime:
            sso.prime_session()
        r = sso.register(f"zqxab{idx}{'p' if prime else 'n'}", f"zqxab{idx}{'p' if prime else 'n'}@example.com", "aa11")
        return f"ok={r.ok} code={r.msg_code!r} ({round(time.time()-t0,1)}s)"
    except Exception as e:
        return f"EXC {type(e).__name__}: {str(e)[:70]} ({round(time.time()-t0,1)}s)"

for idx, s in enumerate(slots[:3], 1):
    print(f"slot{idx} 无prime: {run(False, idx, s)}", flush=True)
    print(f"slot{idx} 有prime: {run(True, idx, s)}", flush=True)
