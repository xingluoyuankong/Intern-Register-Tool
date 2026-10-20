"""服务器端：正确环境下测「带代理的完整注册流程」（prime + register）。

用法：XDG_CACHE_HOME=... DISPLAY=:1 .venv/bin/python -u tools/probes/probe_full_reg.py
"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, time
sys.path.insert(0, "/app/working/intern-register")
import src.sso as S
from src.sso import SSOClient

SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]

# 插桩 _solve_waf
_orig = SSOClient._solve_waf
def _patched(self, r, method="js"):
    print(f"    [solve {method}] challenge={SSOClient._is_waf_challenge(r)}", flush=True)
    try:
        _orig(self, r, method=method)
        print(f"    [solve {method}] OK cookie={bool(self.session.cookies.get('acw_sc__v2'))}", flush=True)
    except Exception as e:
        print(f"    [solve {method}] FAIL {type(e).__name__}: {str(e)[:80]}", flush=True)
        raise
SSOClient._solve_waf = _patched

for idx, s in enumerate(slots[:4], 1):
    sso = SSOClient(proxy=s)
    sso.abandon_on_challenge = False
    t0 = time.time()
    try:
        sso.prime_session()
        r = sso.register(f"zqxfr{idx}", f"zqxfr{idx}@example.com", "aa11")
        print(f"slot{idx}: ok={r.ok} code={r.msg_code!r} msg={r.msg!r} ({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"slot{idx}: EXC {type(e).__name__}: {str(e)[:110]} ({round(time.time()-t0,1)}s)", flush=True)
