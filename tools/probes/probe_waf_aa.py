"""服务器端：抓 aliyun_waf_aa 新挑战页全文，分析其 JS。"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, re, requests
sys.path.insert(0, "/app/working/intern-register")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
h = {"Accept": "application/json, text/plain, */*", "Content-Type": "application/json", "lang": "zh-CN",
     "Origin": "https://sso.openxlab.org.cn", "Referer": "https://sso.openxlab.org.cn/register", "User-Agent": UA}
SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]

sess = requests.Session()
sess.trust_env = False
sess.get("https://sso.openxlab.org.cn/register", headers=h, timeout=15)
r = sess.post("https://sso.openxlab.org.cn/gw/uaa-be/api/v1/register/byEmail", headers=h, timeout=20,
              json={"username": "zqxaa1", "email": "zqxaa1@example.com", "password": "aa11",
                    "source": "discovery", "clientId": "dagw07mkg1bazlxzoy31"})
print("status", r.status_code, "ct", r.headers.get("content-type"), "len", len(r.text), flush=True)
print("has acw_sc__v2:", "acw_sc__v2" in r.text, "has aliyunwaf:", "aliyunwaf" in r.text,
      "has aliyun_waf_aa:", "aliyun_waf_aa" in r.text, flush=True)
print("=== 全文前 1500 ===", flush=True)
print(r.text[:1500], flush=True)
print("=== script 块 ===", flush=True)
for i, m in enumerate(re.finditer(r"<script[^>]*>(.*?)</script>", r.text, re.S | re.I)):
    print(f"--- script{i} ({len(m.group(1))}B) ---", flush=True)
    print(m.group(1).strip()[:600], flush=True)
