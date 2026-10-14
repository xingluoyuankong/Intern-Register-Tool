"""服务器端：同一出口连打，直到重放返回 aliyun_waf_aa 页，落盘全文。"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, requests
sys.path.insert(0, "/app/working/intern-register")
from src.waf_bypass import WafSolver

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
h = {"Accept": "application/json, text/plain, */*", "Content-Type": "application/json", "lang": "zh-CN",
     "Origin": "https://sso.openxlab.org.cn", "Referer": "https://sso.openxlab.org.cn/register", "User-Agent": UA}
SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]
payload = {"username": "zqxforce1", "email": "zqxforce1@example.com", "password": "aa11",
           "source": "discovery", "clientId": "dagw07mkg1bazlxzoy31"}

OUT = "/app/working/intern-register/logs/aa_forced.html"
found = False
for si, s in enumerate(slots, 1):
    if found:
        break
    px = {"http": s, "https": s}
    for att in range(3):
        if found:
            break
        try:
            sess = requests.Session()
            sess.get("https://sso.openxlab.org.cn/register", headers=h, proxies=px, timeout=12)
            r = sess.post("https://sso.openxlab.org.cn/gw/uaa-be/api/v1/register/byEmail", headers=h, proxies=px, timeout=18, json=payload)
            if not ("acw_sc__v2" in r.text or "aliyunwaf" in r.text):
                continue
            ck = WafSolver.get().solve(r.text)
            for k, v in ck.items():
                sess.cookies.set(k, v, domain="sso.openxlab.org.cn", path="/")
            r2 = sess.post("https://sso.openxlab.org.cn/gw/uaa-be/api/v1/register/byEmail", headers=h, proxies=px, timeout=18, json=payload)
            if "aliyun_waf_aa" in r2.text:
                open(OUT, "w", encoding="utf-8").write(r2.text)
                print(f"FOUND slot{si} att{att} len={len(r2.text)} -> {OUT}", flush=True)
                found = True
            else:
                print(f"slot{si} att{att}: ct={r2.headers.get('content-type')} {r2.text[:60]}", flush=True)
        except Exception as e:
            print(f"slot{si} att{att}: EXC {type(e).__name__}", flush=True)
try:
    WafSolver.get().shutdown()
except Exception:
    pass
print("done found=", found, flush=True)
