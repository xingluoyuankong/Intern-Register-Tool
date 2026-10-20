"""WAF 解除探测器（服务器侧）：每 15 分钟探一次 register/byEmail。

放行判据：响应 content-type 是 application/json（无论业务成败）→ WAF 不再拦。
命中后自动跑批量 --count 6 --workers 2，跑完退出。最长守 6 小时。

探测用非法邮箱（@example.invalid）：WAF 放行时业务层直接拒格式，
不消耗注册配额；被拦时返回挑战页 / 405 HTML，一眼可分。
"""
import subprocess
import time

from src import config
from src.crypto_rsa import encrypt_password
from src.sso import SSOClient

URL = f"{config.SSO_GW}/register/byEmail"
INTERVAL = 900          # 15 min
MAX_TRIES = 24          # 6 h
BATCH = ["--count", "6", "--workers", "2"]

sso = SSOClient()
h = sso._headers("/register")


def probe() -> tuple[bool, str]:
    tag = str(int(time.time()))[-8:]
    email = f"wafwatch{tag}@example.invalid"
    payload = {
        "username": f"ww{tag[-6:]}",
        "email": email,
        "password": encrypt_password(email, "Watch!2026x"),
        "source": config.SOURCE,
        "clientId": config.CLIENT_ID,
    }
    r = sso.session.post(URL, headers=h, json=payload, timeout=30)
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        return True, f"json {r.status_code} {r.text[:80]}"
    return False, f"blocked {r.status_code} html len={len(r.text)}"


def main():
    for i in range(1, MAX_TRIES + 1):
        ok, detail = probe()
        stamp = time.strftime("%H:%M:%S")
        verdict = "CLEARED" if ok else "still-blocked"
        print(f"[{stamp}] try {i}/{MAX_TRIES}: {verdict} ({detail})", flush=True)
        if ok:
            print("waf cleared -> launching batch", flush=True)
            subprocess.run([".venv/bin/python", "-u", "run.py", *BATCH], cwd=".")
            return
        time.sleep(INTERVAL)
    print("gave up after 6h — WAF still blocking", flush=True)


if __name__ == "__main__":
    main()
