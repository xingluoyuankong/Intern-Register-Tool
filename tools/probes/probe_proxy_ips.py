import sys
sys.path.insert(0, "/app/working/intern-register")
import requests
from src import config

slots = [l.strip() for l in open(
    "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt",
    encoding="utf-8") if l.strip()]
ips = {}
for i, s in enumerate(slots[:12], 1):
    px = config.proxies(s)
    try:
        ip = requests.get("https://api.ipify.org", proxies=px, timeout=15).text
    except Exception as e:
        ip = f"FAIL {type(e).__name__}"
    ips.setdefault(ip, []).append(i)
    print(f"slot{i}: {ip}", flush=True)

print("\n=== 出口 IP 汇总 ===", flush=True)
for ip, idxs in ips.items():
    print(f"{ip}  → 槽位 {idxs}", flush=True)
