import sys
sys.path.insert(0, "/app/working/intern-register")
import requests
from src import config

slots = [l.strip() for l in open(
    "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt",
    encoding="utf-8") if l.strip()]
print("槽位:", len(slots), flush=True)
for i, s in enumerate(slots[:3], 1):
    px = config.proxies(s)
    for name, url in [("ipify", "https://api.ipify.org"),
                      ("sso", "https://sso.openxlab.org.cn/register")]:
        try:
            r = requests.get(url, proxies=px, timeout=20)
            print(f"slot{i} {name}: {r.status_code} {r.text[:40]!r}", flush=True)
        except Exception as e:
            print(f"slot{i} {name}: FAIL {type(e).__name__}: {str(e)[:70]}",
                  flush=True)
