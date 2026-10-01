"""curl_cffi（Chrome TLS 指纹）协议注册探针 —— 验证指纹假设。

对照矩阵（每格一发，非法邮箱不耗配额）：
  A. curl_cffi impersonate=chrome + mihomo:7901（Resin）
  B. curl_cffi impersonate=chrome + 直连
  C. requests（python 指纹，对照组）+ mihomo:7901
"""
import sys
import time

sys.path.insert(0, ".")
from curl_cffi import requests as cffi  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

URL = f"{config.SSO_GW}/register/byEmail"
tag = str(int(time.time()))[-8:]


def mkpayload(dom: str) -> dict:
    email = f"cffitest{tag}{dom}"
    return {
        "username": f"cf{tag[-6:]}{dom[:1]}",
        "email": email,
        "password": encrypt_password(email, "Watch!2026x"),
        "source": config.SOURCE,
        "clientId": config.CLIENT_ID,
    }


sso = SSOClient()
h = sso._headers("/register")

cases = [
    ("A cffi+7901", "chrome", "http://127.0.0.1:7901"),
    ("B cffi+direct", "chrome", None),
    ("C requests+7901", None, "http://127.0.0.1:7901"),
]
for i, (label, impersonate, proxy) in enumerate(cases):
    payload = mkpayload(["@hotmail.com", "@gmail.com", "@outlook.com"][i])
    t0 = time.time()
    try:
        if impersonate:
            r = cffi.post(URL, json=payload, headers=h,
                          impersonate=impersonate, timeout=30,
                          proxies={"http": proxy, "https": proxy}
                          if proxy else None)
        else:
            r = cffi.post(URL, json=payload, headers=h, timeout=30,
                          proxies={"http": proxy, "https": proxy})
        ct = (r.headers.get("content-type") or "")[:24]
        ch = "CHALLENGE" if "acw_sc__v2" in r.text else "pass"
        print(f"[{label}] {time.time() - t0:.1f}s {r.status_code} {ct} "
              f"{ch} | {r.text[:100]}", flush=True)
    except Exception as ex:  # noqa: BLE001
        print(f"[{label}] {time.time() - t0:.1f}s EXC "
              f"{type(ex).__name__}: {str(ex)[:120]}", flush=True)
    time.sleep(2)
