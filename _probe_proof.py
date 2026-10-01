"""自证：注册请求是否真的走代理 —— 出口 IP vs 服务器公网 IP 对照。"""
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

SLOT = ("http://Link.rl2.dmpZa2t2SENyUlVjRFZ3WZieXZLTMML2:"
        "@resin-proxy.xzxyuan.ccwu.cc:2268")
SOURCES = ["https://api.ipify.org?format=json", "https://httpbin.org/ip",
           "https://icanhazip.com"]


def log(*a):
    print(f"[{time.time() % 10000:07.2f}]", *a, flush=True)


# 1) 服务器自身公网 IP（直连，无代理）
direct = requests.Session()
for u in SOURCES:
    try:
        r = direct.get(u, timeout=12)
        log("server DIRECT ip:", r.text.strip()[:60])
        break
    except Exception as ex:  # noqa: BLE001
        log("direct src fail", type(ex).__name__)

# 2) 走槽位的出口 IP（与注册请求同一 session）
s = requests.Session()
s.proxies = {"http": SLOT, "https": SLOT}
for u in SOURCES:
    try:
        r = s.get(u, timeout=15)
        log("slot EXIT ip:", r.text.strip()[:60])
        break
    except Exception as ex:  # noqa: BLE001
        log("slot src fail", type(ex).__name__)

# 3) 同一 session 走槽位发起注册
sso = SSOClient(proxy=SLOT)
tag = str(int(time.time()))[-6:]
email = f"proof{tag}@example.invalid"
payload = {
    "username": f"pf{tag}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
sso.prime_session()
try:
    r = sso._post("/register/byEmail", payload)
    ch = "CHALLENGE" if "acw_sc__v2" in r.text else "pass"
    log(f"register(via slot) -> {r.status_code} {ch} | {r.text[:100]}")
except Exception as ex:  # noqa: BLE001
    log("register EXC", type(ex).__name__, str(ex)[:120])
