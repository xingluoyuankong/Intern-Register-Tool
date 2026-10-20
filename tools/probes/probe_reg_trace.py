"""服务器端：插桩 SSOClient 注册流程，逐步打印 WAF 处理。

用法：.venv/bin/python -u tools/probes/probe_reg_trace.py
"""
import sys, time
sys.path.insert(0, "/app/working/intern-register")
import src.sso as S
from src.sso import SSOClient

SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]

# 插桩 _solve_waf
_orig_solve = SSOClient._solve_waf
def _patched(self, r, method="js"):
    print(f"    [_solve_waf method={method}] challenge={S.SSOClient._is_waf_challenge(r)}", flush=True)
    try:
        _orig_solve(self, r, method=method)
        print(f"    [_solve_waf {method}] OK cookie={self.session.cookies.get('acw_sc__v2')}", flush=True)
    except Exception as e:
        print(f"    [_solve_waf {method}] FAIL {type(e).__name__}: {str(e)[:80]}", flush=True)
        raise
SSOClient._solve_waf = _patched

for idx, s in enumerate(slots[:3], 1):
    print(f"=== slot{idx} ===", flush=True)
    sso = SSOClient(proxy=s)
    sso.abandon_on_challenge = False
    t0 = time.time()
    try:
        r = sso.register(f"zqxtr{idx}", f"zqxtr{idx}@example.com", "aa11")
        print(f"  => ok={r.ok} code={r.msg_code!r} msg={r.msg!r} ({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"  => EXC {type(e).__name__}: {str(e)[:120]} ({round(time.time()-t0,1)}s)", flush=True)
