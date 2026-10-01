"""纯代理对照实验（本机/服务器同跑）：打印出口 IP + 注册结果。

用法：python _probe_pair.py [--real]
  不带 --real：非法邮箱（不耗配额），只看穿透形态
  带 --real  ：真邮箱走完整注册（会用掉一个配额）
"""
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

REAL = "--real" in sys.argv
SLOT = ("http://Link.rl2.U3lzZmdnX25VdHFVQWdudFv5et_1t6OX:"
        "@resin-proxy.xzxyuan.ccwu.cc:2268")


def log(*a):
    print(f"[{time.time() % 10000:07.2f}]", *a, flush=True)


def exit_ip(sess: requests.Session) -> str:
    try:
        return sess.get("https://api.ipify.org?format=json",
                        timeout=20).json()["ip"]
    except Exception as ex:  # noqa: BLE001
        return f"ERR:{type(ex).__name__}"


s = requests.Session()
s.proxies = {"http": SLOT, "https": SLOT}
log("exit IP (直连会话经代理):", exit_ip(s))

sso = SSOClient(proxy=SLOT)          # 全程只走这个槽位
tag = str(int(time.time()))[-8:]
dom = "@hotmail.com" if REAL else "@example.invalid"
email = f"pair{tag}{dom}"
payload = {
    "username": f"pr{tag[-6:]}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}

log("prime_session (GET /register)...")
try:
    sso.prime_session()
    log("prime ok; session cookies:", sorted(c.name for c in sso.session.cookies))
except Exception as ex:  # noqa: BLE001
    log("prime FAIL", type(ex).__name__, str(ex)[:100])

try:
    r = sso._post("/register/byEmail", payload)
    ct = (r.headers.get("content-type") or "")[:20]
    ch = "CHALLENGE" if "acw_sc__v2" in r.text else "pass"
    log(f"register -> {r.status_code} {ct} {ch} | {r.text[:150]}")
except Exception as ex:  # noqa: BLE001
    log("register EXC", type(ex).__name__, str(ex)[:150])

log("exit IP after:", exit_ip(s))
