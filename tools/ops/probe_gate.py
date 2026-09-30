"""产线闸门：一发非法邮箱探测（GET 种会话 → POST），穿透才放行批量。

退出码：0 = 穿透（WAF 放行 / 业务层响应），1 = 被拦（挑战页/405）。
非法邮箱（example.invalid）业务层直接拒格式 —— 不消耗注册配额。
watch.sh 每轮 kick 前调用；被拦时不跑批量（避免给 WAF 喂更多学习样本）。
"""
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

sso = SSOClient()
H = sso._headers("/register")

# 会话链：GET 注册页
try:
    sso.session.get(f"{config.SSO_BASE}/register", headers=H, timeout=30)
except requests.RequestException:
    pass

tag = str(int(time.time()))[-8:]
email = f"gate{tag}@example.invalid"
payload = {
    "username": f"gt{tag[-6:]}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}

ok = False
for _ in range(3):
    try:
        r = sso.session.post(f"{config.SSO_GW}/register/byEmail",
                             headers=H, json=payload, timeout=30)
    except requests.RequestException as ex:
        print(f"gate: network {type(ex).__name__}", flush=True)
        sys.exit(1)
    if "acw_sc__v2" in r.text:
        sso._solve_waf(r)          # 算法解一次再探
        time.sleep(0.4)
        continue
    ct = (r.headers.get("content-type") or "")
    # 🔴 必须是业务层 200 JSON 才算真放行：429 虽然"穿过了 WAF"，但写接口
    #    速率窗口没恢复，跑批量 8 个号全会以 429 失败（2026-09-30 实测）。
    if r.status_code == 200 and "application/json" in ct:
        ok = True
        print(f"gate: CLEARED {r.status_code} {r.text[:60]}", flush=True)
    else:
        print(f"gate: blocked {r.status_code} {ct[:20]} {r.text[:40]}", flush=True)
    break
else:
    print("gate: challenge loop (3x)", flush=True)

sys.exit(0 if ok else 1)
