"""代理可用性筛选（POST 级）：直连基线 + 每槽位真实 POST 探测。

🔴 为什么不能用 GET 探：WAF 对 GET/POST 的下发策略不同 —— GET 返回
   普通页面（甚至 405），POST 才可能收到挑战页。用 GET 筛出来的"可用"
   代理，注册时照样被挑战（2026-09-30 实测的教训）。

筛选判据（非法邮箱，业务层拒格式，不消耗注册配额）：
    CLEAR  业务层 JSON（200/429 都算穿透 WAF，429 只是速率窗口）
    CHAL   仍是挑战页（算法/JS 没解掉 OR 该出口被特殊对待）
    DEAD   TLS/连接层失败（SSLError、超时 —— 出口到目标站链路不通）
用法：python tools/ops/screen_slots.py [--direct]
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SLOTS = ROOT / ".workbuddy-ai" / "proxypool" / "slots.txt"
ACTIVE = ROOT / ".workbuddy-ai" / "proxypool" / "slots_active.txt"
ENV = ROOT / ".env"
URL = f"{config.SSO_GW}/register/byEmail"


def user_of(slot: str) -> str:
    return slot.split("://", 1)[-1].rsplit("@", 1)[0].split(":", 1)[0]


def probe(slot: str, label: str) -> tuple[str, str, str]:
    """返回 (verdict, exit_ip, slot)。"""
    tag = str(int(time.time()))[-6:]
    # 🔴 必须用**与真实注册一致**的邮箱域名探测，不能用 example.invalid：
    #    非法邮箱会被业务层秒回（格式错误），WAF 根本不拦 → 判成 CLEAR，
    #    但真邮箱走同一出口时照样被挑战。实测后果：screen 报 6 个可用、
    #    真注册 8 号全部秒失败（10-01 00:53 轮）。改用 outlook.com 后
    #    CLEAR 才等价于"这个出口真能注册"。
    email = f"sc{label}{tag}@outlook.com"
    payload = {
        "username": f"sc{label}{tag}",
        "email": email,
        "password": encrypt_password(email, "Watch!2026x"),
        "source": config.SOURCE,
        "clientId": config.CLIENT_ID,
    }
    sso = SSOClient(proxy=slot or None, timeout=15)
    try:
        sso.prime_session()
        r = sso._post("/register/byEmail", payload)
    except requests.RequestException as ex:
        return f"DEAD:{type(ex).__name__}", "-", slot
    except Exception as ex:  # noqa: BLE001
        return f"ERR:{type(ex).__name__}", "-", slot

    # 取出口 IP（POST 之后，同一 session 语义）
    ip = "-"
    try:
        ip = sso.session.get("https://api.ipify.org?format=json",
                             timeout=10).json()["ip"]
    except Exception:  # noqa: BLE001
        pass

    if "acw_sc__v2" in r.text:
        return "CHAL", ip, slot
    if r.status_code in (200, 429):
        return "CLEAR", ip, slot
    return f"HTTP{r.status_code}", ip, slot


def main() -> int:
    if "--direct" in sys.argv:
        v, ip, _ = probe("", "D")
        print(f"direct(no proxy) -> {v} exit={ip}", flush=True)

    slots = [ln.strip() for ln in SLOTS.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    results = []
    # 🔴 池子只有 10 条（随机用户名实测 ProxyError，派生不了），
    #    所以提速只能靠压缩单轮耗时：全量并发 + 短超时。
    #    并发 10 / 超时 15s → 一轮 ~1.5 分钟（原 4→5 分钟），
    #    单位时间撞窗口的次数翻 3 倍 —— 命中率不变但产出速率上去了。
    with ThreadPoolExecutor(max_workers=len(slots)) as ex:
        for v, ip, slot in ex.map(
                lambda p: probe(p[1], p[0]), list(enumerate(slots))):
            results.append((v, ip, slot))
            print(f"{v:<22} {ip:<16} {user_of(slot)[:18]}", flush=True)

    good = [(ip, slot) for v, ip, slot in results if v == "CLEAR" and ip != "-"]
    ACTIVE.write_text("\n".join(s for _, s in good) + "\n", encoding="utf-8")
    mapping = ",".join(f"{user_of(s)}={ip}" for ip, s in good)
    lines = [(f"IR_SLOT_EGRESS_IPS={mapping}"
              if ln.startswith("IR_SLOT_EGRESS_IPS") else ln)
             for ln in ENV.read_text(encoding="utf-8").splitlines()]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nusable(POST-CLEAR) {len(good)}/{len(slots)} -> slots_active.txt",
          flush=True)
    return len(good)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
