"""对比：直连拿到的挑战页 vs 走代理拿到的挑战页 —— 是否不同版本？

同一台机器、同一脚本，唯一变量 = 出口 IP。结构/长度/函数不同 ⇒
WAF 按来源段下发不同挑战版本，单一算法不可能通吃。
"""
import hashlib
import re
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

SLOT = ("http://Link.rl2.U3lzZmdnX25VdHFVQWdudFv5et_1t6OX:"
        "@resin-proxy.xzxyuan.ccwu.cc:2268")
URL = f"{config.SSO_GW}/register/byEmail"


def fetch(proxy=None, label=""):
    sso = SSOClient(proxy=proxy)
    tag = str(int(time.time()))[-6:]
    email = f"ch{label}{tag}@example.invalid"
    payload = {
        "username": f"ch{label}{tag}",
        "email": email,
        "password": encrypt_password(email, "Watch!2026x"),
        "source": config.SOURCE,
        "clientId": config.CLIENT_ID,
    }
    try:
        r = sso.session.post(URL, headers=sso._headers("/register"),
                             json=payload, timeout=25)
    except Exception as ex:  # noqa: BLE001
        print(f"[{label}] EXC {type(ex).__name__}: {str(ex)[:100]}", flush=True)
        return None
    body = r.text
    m = re.search(r"arg1='([0-9A-Fa-f]+)'", body)
    print(f"[{label}] status={r.status_code} len={len(body)} "
          f"arg1len={len(m.group(1)) if m else '-'} "
          f"md5={hashlib.md5(body.encode()).hexdigest()[:12]}", flush=True)
    if m:
        print(f"[{label}] arg1={m.group(1)[:48]}", flush=True)
        # 混淆函数名/结构指纹
        fns = sorted(set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", body)))
        print(f"[{label}] fns={fns[:8]}", flush=True)
        print(f"[{label}] script_len="
              f"{len(re.search(r'<script>(.*?)</script>', body, re.S).group(1)) if re.search(r'<script>(.*?)</script>', body, re.S) else '-'}", flush=True)
    return body


print("=== 直连（本机出口）===", flush=True)
a = fetch(None, "direct")
time.sleep(2)
print("=== 代理（Resin 出口）===", flush=True)
b = fetch(SLOT, "proxy")
if a and b:
    print("identical:", a == b, flush=True)
