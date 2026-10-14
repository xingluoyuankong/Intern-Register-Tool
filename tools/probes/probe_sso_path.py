"""用**产线同一条代码路径**（SSOClient.register → browser_post）逐个槽位注册，
对比 tools/probes/probe_slot_health.py（直接调 browser_post）的结果。

两者若不一致，说明差异在 SSOClient/pipeline 这一层（而不是出口本身）。
"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, time
sys.path.insert(0, "/app/working/intern-register")

from src.sso import SSOClient, RateLimitedError

SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]
print(f"槽位数 {len(slots)}  REGISTER_VIA_BROWSER 生效中", flush=True)

want = [int(x) for x in (sys.argv[1:] or ["2"])]     # 默认只测 slot2
for idx in want:
    s = slots[idx - 1]
    t0 = time.time()
    u = f"zqxsso{9000+idx}"
    em = f"{u}@example.com"
    try:
        sso = SSOClient(proxy=s)
        r = sso.register(u, em, "Aa11bb22!")
        print(f"slot{idx}: ok={r.ok} uid={r.sso_uid!r} code={r.msg_code!r} "
              f"msg={r.msg!r} ({round(time.time()-t0,1)}s)", flush=True)
    except RateLimitedError as e:
        print(f"slot{idx}: 429限流 {e} ({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"slot{idx}: 异常 {type(e).__name__}: {str(e)[:120]} "
              f"({round(time.time()-t0,1)}s)", flush=True)
