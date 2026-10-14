"""服务器端：多槽位重试，跑到重放返回 aliyun_waf_aa 页，dump 全文。"""
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
payload = {"username": "zqxaa1", "email": "zqxaa1@example.com", "password": "aa11",
           "source": "discovery", "clientId": "dagw07mkg1bazlxzoy31"}

for si, s in enumerate(slots, 1):
    px = {"http": s, "https": s}
    try:
        sess = requests.Session()
        sess.get("https://sso.openxlab.org.cn/register", headers=h, proxies=px, timeout=15)
        r = sess.post("https://sso.openxlab.org.cn/gw/uaa-be/api/v1/register/byEmail", headers=h, proxies=px, timeout=20, json=payload)
        if not ("acw_sc__v2" in r.text or "aliyunwaf" in r.text):
            print(f"slot{si}: 首POST非挑战 {r.status_code}", flush=True); continue
        ck = WafSolver.get().solve(r.text)
        for k, v in ck.items():
            sess.cookies.set(k, v, domain="sso.openxlab.org.cn", path="/")
        r2 = sess.post("https://sso.openxlab.org.cn/gw/uaa-be/api/v1/register/byEmail", headers=h, proxies=px, timeout=20, json=payload)
        print(f"slot{si}: 重放 {r2.status_code} ct={r2.headers.get('content-type')} len={len(r2.text)}", flush=True)
        if "aliyun_waf_aa" in r2.text:
            open("/tmp/aa_dump.html", "w", encoding="utf-8").write(r2.text)
            print("DUMPED", len(r2.text), flush=True)
            break
        elif "json" in (r2.headers.get("content-type") or ""):
            print("业务层:", r2.text[:150], flush=True); break
    except Exception as e:
        print(f"slot{si}: FAIL {type(e).__name__}", flush=True)
        continue
try:
    WafSolver.get().shutdown()
except Exception:
    pass
