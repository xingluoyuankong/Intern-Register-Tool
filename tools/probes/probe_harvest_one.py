"""单账号下游诊断：挑一个「已注册未领 key」账号，verbose 登录 + 只读 + 建 key。

用途：收割批量跑不动时，用最小样本定位卡点（登录？验证码？只读接口？）。
输出**流式**（verbose=True），不像 run_downstream 那样攒到批末才吐。

用法：
    .venv/bin/python -u tools/probes/probe_harvest_one.py            # 第一个目标
    .venv/bin/python -u tools/probes/probe_harvest_one.py --screens  # 落关键截图
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import ledger                     # noqa: E402
from src.browser import BrowserSession     # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--email", default="", help="指定账号邮箱（默认取第一个目标）")
ap.add_argument("--login-as", default="",
                help="用这个值填登录框（默认用邮箱）。用于测「超长邮箱改用 SSO 用户名登录」")
ap.add_argument("--proxy", default="", help="浏览器出口代理（默认直连）")
ap.add_argument("--screens", action="store_true", help="落关键步骤截图到 logs/")
ap.add_argument("--attempts", type=int, default=1)
args = ap.parse_args()


def has(d, k):
    return bool((d.get(k) or "").strip())


recs = ledger.load_existing(ledger.ledger_path())
tgt = [d for d in recs
       if has(d, "sso_uid") and not has(d, "api_key") and has(d, "password")]
if args.email:
    tgt = [d for d in tgt if d["email"] == args.email] or \
          [d for d in recs if d.get("email") == args.email]
print(f"候选 {len(tgt)} 个", flush=True)
if not tgt:
    print("无可收割账号", flush=True)
    sys.exit(1)
d = tgt[0]
# 与 run_downstream / pipeline 同一套登录判据（超长邮箱自动改用 SSO 用户名）
from src.pipeline import login_identifier  # noqa: E402
login_as = args.login_as or login_identifier(d.get("email", ""),
                                             d.get("username", ""))
print(f"TARGET email={d['email']} user={d.get('username')} uid={d.get('sso_uid')} "
      f"login_as={login_as}", flush=True)

prefix = "logs/probeshot" if args.screens else None
t0 = time.time()
with BrowserSession(headless=True) as s:
    r = s.login(login_as, d["password"], verbose=True, attempts=args.attempts,
                proxy=(args.proxy or None),
                screenshot_prefix=prefix)
    dt = round(time.time() - t0, 1)
    print(f"LOGIN ok={r.ok} reason={r.reason!r} jwt={len(r.jwt or '')}B "
          f"captcha={r.captcha_stage} 耗时={dt}s", flush=True)
    if not r.ok:
        print("登录失败，停止", flush=True)
        sys.exit(2)

    from src.discovery import DiscoveryClient
    dc = DiscoveryClient(jwt=r.jwt, cookies=r.cookies)
    print("只读 getUserInfo ...", flush=True)
    print("  info:", dc.get_user_info(), flush=True)
    print("只读 balance ...", flush=True)
    print("  bal:", dc.balance(), flush=True)
    print("只读 list_keys ...", flush=True)
    ks = dc.list_keys()
    print(f"  keys={len(ks)} {[k.get('name') for k in ks]}", flush=True)
    ak = dc.ensure_key("default")
    print(f"ensure_key -> key={'<有明文>' if ak.key else '<空(命中已有)>'} "
          f"id={ak.id} masked={ak.masked_key}", flush=True)
print("DONE", flush=True)
