import sys
sys.path.insert(0, "/app/working/intern-register")
import requests
from src import config

URL = "https://sso.openxlab.org.cn/register"

# ① 服务器直连（不走代理）
try:
    r = requests.get(URL, timeout=20)
    print(f"DIRECT: {r.status_code} len={len(r.text)}", flush=True)
except Exception as e:
    print(f"DIRECT: FAIL {type(e).__name__}: {str(e)[:70]}", flush=True)

# ② 遍历所有槽位
slots = [l.strip() for l in open(
    "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt",
    encoding="utf-8") if l.strip()]
ok, fail = [], {}
for i, s in enumerate(slots, 1):
    px = config.proxies(s)
    try:
        r = requests.get(URL, proxies=px, timeout=18)
        ok.append(i)
        print(f"slot{i}: {r.status_code} len={len(r.text)}", flush=True)
    except Exception as e:
        k = type(e).__name__
        fail[k] = fail.get(k, 0) + 1

print(f"\n=== 可达 {len(ok)}/{len(slots)}  成功槽位: {ok} ===", flush=True)
print(f"=== 失败类型: {fail} ===", flush=True)
