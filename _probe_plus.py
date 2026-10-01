"""验证：邮箱的 `+别名` 是否被 WAF 当批量特征。

对照同一槽位：
  A  xxx+alias@outlook.com  （现行 run.py 格式）
  B  xxx{tag}@outlook.com   （无 +）
看两者 register 响应差异（挑战 / 业务 JSON）。
"""
import sys
import time

sys.path.insert(0, ".")
from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

# 用真 CLEAR 的槽位
SLOTS = [
    "http://Link.rl2.SDEyemVHRk5relpaTUNKb34h7_LyuuOm:"
    "@resin-proxy.xzxyuan.ccwu.cc:2268",
    "http://Link.rl2.SkUzZVRSblZlVmU5OWZDQiYYLTrLvoe7:"
    "@resin-proxy.xzxyuan.ccwu.cc:2268",
]


def try_one(slot: str, email: str, label: str):
    sso = SSOClient(proxy=slot, timeout=20)
    payload = {
        "username": f"{label}{str(int(time.time()))[-5:]}",
        "email": email,
        "password": encrypt_password(email, "Watch!2026x"),
        "source": config.SOURCE,
        "clientId": config.CLIENT_ID,
    }
    sso.prime_session()
    try:
        r = sso._post("/register/byEmail", payload)
        ch = "CHALLENGE" if "acw_sc__v2" in r.text else "pass"
        print(f"[{label}] {email[:42]:<44} -> {r.status_code} {ch} "
              f"| {r.text[:70]}", flush=True)
    except Exception as ex:  # noqa: BLE001
        print(f"[{label}] {email[:42]:<44} -> EXC {type(ex).__name__} "
              f"{str(ex)[:60]}", flush=True)


for i, slot in enumerate(SLOTS):
    tag = str(int(time.time()))[-7:]
    try_one(slot, f"teenawelles799+{tag}@outlook.com", f"A{i}")
    time.sleep(1)
    try_one(slot, f"probe{tag}@outlook.com", f"B{i}")
    time.sleep(2)
