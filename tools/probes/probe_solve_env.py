"""服务器端：测 WafSolver.solve（假页面解法，产线用的就是它）。"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, time, requests
sys.path.insert(0, "/app/working/intern-register")
from src.waf_bypass import WafSolver

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
h = {"Accept": "application/json, text/plain, */*", "Content-Type": "application/json", "lang": "zh-CN",
     "Origin": "https://sso.openxlab.org.cn", "Referer": "https://sso.openxlab.org.cn/register", "User-Agent": UA}
SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]

# 直连拿一个挑战页（不联网也能解，但挑战页得先有）
ch = None
for s in [None] + slots[:4]:
    px = {"http": s, "https": s} if s else None
    try:
        sess = requests.Session()
        sess.trust_env = False
        sess.get("https://sso.openxlab.org.cn/register", headers=h, proxies=px, timeout=15)
        r = sess.post("https://sso.openxlab.org.cn/gw/uaa-be/api/v1/register/byEmail", headers=h, proxies=px, timeout=20,
                      json={"username": "zqxsv1", "email": "zqxsv1@example.com", "password": "aa11",
                            "source": "discovery", "clientId": "dagw07mkg1bazlxzoy31"})
        if "aliyunwaf" in r.text or "acw_sc__v2" in r.text:
            ch = r.text
            print("拿到挑战页", len(ch), "from", "direct" if s is None else "proxy", flush=True)
            break
    except Exception as e:
        print("拿挑战页失败", type(e).__name__, flush=True)
if not ch:
    print("没拿到挑战页，退出", flush=True)
    sys.exit()

t0 = time.time()
try:
    ck = WafSolver.get().solve(ch)
    print(f"solve -> {ck} ({round(time.time()-t0,1)}s)", flush=True)
except Exception as e:
    print(f"solve FAIL {type(e).__name__}: {str(e)[:150]} ({round(time.time()-t0,1)}s)", flush=True)
try:
    WafSolver.get().shutdown()
except Exception:
    pass
