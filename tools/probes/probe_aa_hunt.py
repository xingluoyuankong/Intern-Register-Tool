"""服务器端：狂试，抓任意非 JSON 的重放响应（含 aliyun_waf_aa 页）并落盘。"""
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
payload = {"username": "zqxhunt1", "email": "zqxhunt1@example.com", "password": "aa11",
           "source": "discovery", "clientId": "dagw07mkg1bazlxzoy31"}

found = False
for rep in range(3):
    for si, s in enumerate(slots, 1):
        if found:
            break
        px = {"http": s, "https": s}
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
            ct = r2.headers.get("content-type") or ""
            if "json" in ct:
                print(f"rep{rep} slot{si}: 业务层 {r2.text[:90]}", flush=True)
            elif "text/html" in ct:
                print(f"rep{rep} slot{si}: 非JSON页 len={len(r2.text)} head={r2.text[:150]!r}", flush=True)
                open("/tmp/aa_dump.html", "w", encoding="utf-8").write(r2.text)
                print("DUMPED", len(r2.text), flush=True)
                found = True
        except Exception as e:
            continue
try:
    WafSolver.get().shutdown()
except Exception:
    pass
print("done, found=", found, flush=True)
