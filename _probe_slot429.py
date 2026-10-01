"""429 归因：换不同出口槽位各发一发，判断是否出口 IP 维度限流。"""
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

SLOTS = [
    ("U3lz", "http://Link.rl2.U3lzZmdnX25VdHFVQWdudFv5et_1t6OX:"
             "@resin-proxy.xzxyuan.ccwu.cc:2268"),
    ("OXZH", "http://Link.rl2.OXZHQTVhUTNnTFBfRXlxbD99x3RClnsA:"
             "@resin-proxy.xzxyuan.ccwu.cc:2268"),
    ("dmpZ", "http://Link.rl2.dmpZa2t2SENyUlVjRFZ3WZieXZLTMML2:"
             "@resin-proxy.xzxyuan.ccwu.cc:2268"),
]


def log(*a):
    print(f"[{time.time() % 10000:07.2f}]", *a, flush=True)


def exit_ip(slot: str) -> str:
    s = requests.Session()
    s.proxies = {"http": slot, "https": slot}
    try:
        return s.get("https://api.ipify.org?format=json", timeout=20).json()["ip"]
    except Exception as ex:  # noqa: BLE001
        return f"ERR:{type(ex).__name__}"


for name, slot in SLOTS:
    ip = exit_ip(slot)
    sso = SSOClient(proxy=slot)
    tag = str(int(time.time()))[-6:]
    email = f"sl{name}{tag}@example.invalid"
    payload = {
        "username": f"sl{name}{tag}",
        "email": email,
        "password": encrypt_password(email, "Watch!2026x"),
        "source": config.SOURCE,
        "clientId": config.CLIENT_ID,
    }
    try:
        sso.prime_session()
        r = sso._post("/register/byEmail", payload)
        ch = "CHALLENGE" if "acw_sc__v2" in r.text else "pass"
        log(f"[{name}] exit={ip:<16} -> {r.status_code} {ch} | {r.text[:70]}")
    except Exception as ex:  # noqa: BLE001
        log(f"[{name}] exit={ip:<16} -> EXC {type(ex).__name__}: {str(ex)[:80]}")
    time.sleep(2)
