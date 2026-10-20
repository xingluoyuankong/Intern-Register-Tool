"""槽位体检：regloop 停掉并冷却后，逐个槽位打一次 register/byEmail，
记录每个出口当前的真实状态（成功 / 429 / 被挑战 / 异常）。

用途：判断 429 是「出口被烧（按 IP，冷却后能恢复）」还是「池子整体不可用」。
浏览器路径（browser_post）已能过 WAF，所以用它隔离 WAF 变量。
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


def mk(i):
    email = f"zqxh{7000 + i}@example.com"
    return {
        "username": f"zqxh{7000 + i}",
        "email": email,
        "password": encrypt_password(email, "Aa11bb22!"),
        "source": "discovery",
        "clientId": "dagw07mkg1bazlxzoy31",
    }


def classify(status, text):
    if "aliyun_waf_aa" in text:
        return "AA滑块页(未过)"
    if "acw_sc__v2" in text:
        return "ACW挑战页(未过)"
    if status == 429 or "too many request" in text.lower():
        return "429限流"
    if status == 200:
        if '"msgCode":"10000"' in text or '"success":true' in text.replace(" ", ""):
            return "✅注册成功"
        return f"业务响应:{text[:70]}"
    return f"其他:{text[:70]}"


slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]
print(f"槽位数 {len(slots)}", flush=True)
ok = 0
for idx, s in enumerate(slots, 1):
    t0 = time.time()
    try:
        res = WafSolver.get().browser_post(f"{GW}/register/byEmail", mk(idx), proxy=s)
        st = res.get("status")
        tx = str(res.get("text") or "")
        k = classify(st, tx)
        if k.startswith("✅"):
            ok += 1
        print(f"slot{idx}: {k} ({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"slot{idx}: 异常 {type(e).__name__}: {str(e)[:90]} ({round(time.time()-t0,1)}s)", flush=True)
    time.sleep(3)
print(f"=== 汇总: {ok}/{len(slots)} 个槽位注册成功 ===", flush=True)

try:
    WafSolver.get().shutdown()
except Exception:
    pass
