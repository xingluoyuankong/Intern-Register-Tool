"""服务器端：浏览器解出 cookie 后，重放能否穿过 WAF。"""
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
payload = {"username": "zqxsr1", "email": "zqxsr1@example.com", "password": "aa11",
           "source": "discovery", "clientId": "dagw07mkg1bazlxzoy31"}

for si, s in enumerate([None] + slots[:3], 1):
    px = {"http": s, "https": s} if s else None
    tag = "direct" if s is None else f"slot{si-1}"
    try:
        sess = requests.Session()
        sess.trust_env = False
        sess.get("https://sso.openxlab.org.cn/register", headers=h, proxies=px, timeout=15)
        r = sess.post("https://sso.openxlab.org.cn/gw/uaa-be/api/v1/register/byEmail", headers=h, proxies=px, timeout=20, json=payload)
        if not ("aliyunwaf" in r.text or "acw_sc__v2" in r.text):
            print(f"{tag}: 首 POST 非挑战 {r.status_code} {r.text[:60]}", flush=True); continue
        t0 = time.time()
        ck = WafSolver.get().solve(r.text)
        for k, v in ck.items():
            sess.cookies.set(k, v, domain="sso.openxlab.org.cn", path="/")
        r2 = sess.post("https://sso.openxlab.org.cn/gw/uaa-be/api/v1/register/byEmail", headers=h, proxies=px, timeout=20, json=payload)
        kind = "WAF挑战" if ("aliyunwaf" in r2.text or "acw_sc__v2" in r2.text) else r2.text[:80].replace("\n", " ")
        print(f"{tag}: solve={ck} ({round(time.time()-t0,1)}s) → 重放 {r2.status_code} {kind}", flush=True)
    except Exception as e:
        print(f"{tag}: FAIL {type(e).__name__}: {str(e)[:90]}", flush=True)
try:
    WafSolver.get().shutdown()
except Exception:
    pass
