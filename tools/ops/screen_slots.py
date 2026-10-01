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
    # 🔴 Resin 出口是**轮询**不是固定绑定：一次连接失败 ≠ 槽位死，
    #    可能只是轮到了被标记的出口。失败就**新会话重试**（最多 2 次），
    #    每次会话独立 → 出口重新轮询，命中好出口就过了。
    last_exc = None
    for _attempt in range(2):
        sso = SSOClient(proxy=slot or None, timeout=15)
        try:
            sso.prime_session()
            r = sso._post("/register/byEmail", payload)
            break
        except requests.RequestException as ex:
            last_exc = ex
            time.sleep(1)
        except Exception as ex:  # noqa: BLE001
            last_exc = ex
            time.sleep(1)
    else:
        return f"DEAD:{type(last_exc).__name__}x3", "-", slot

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
    # 🔴 405 = WAF 判这个出口高风险 → DEAD（不要当 CLEAR 让注册去撞）
    if r.status_code == 405:
        return "DEAD:HTTP405", ip, slot
    return f"HTTP{r.status_code}", ip, slot


def main() -> int:
    if "--direct" in sys.argv:
        v, ip, _ = probe("", "D")
        print(f"direct(no proxy) -> {v} exit={ip}", flush=True)

    slots = [ln.strip() for ln in SLOTS.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    results = []
    # 🔴 并发必须限流：池子从 10 扩到 50 后，全量并发会把 Resin 网关打爆
    #    → 大量 ProxyError 被误判成 DEAD（实测 50 并发只剩 1 个 CLEAR，
    #      限到 8 并发后同一批槽位状态正常）。宁可多花点时间分批筛。
    workers = min(len(slots), 12)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for v, ip, slot in ex.map(
                lambda p: probe(p[1], p[0]), list(enumerate(slots))):
            results.append((v, ip, slot))
            print(f"{v:<22} {ip:<16} {user_of(slot)[:18]}", flush=True)

    good = [(ip, slot) for v, ip, slot in results if v == "CLEAR" and ip != "-"]
    # 🔴 **按出口 IP 去重**：Resin 网关会给不同订阅链接分配同一个出口 IP，
    #    不去重的话注册时连续几单其实是同一个 IP 在打 → 同 IP 高频被 WAF
    #    按批量拦（用户 10-01 点名的问题）。每个 IP 只留一条槽位。
    seen_ip: dict[str, str] = {}
    for ip, slot in good:
        seen_ip.setdefault(ip, slot)
    dedup = sorted(seen_ip.items())
    ACTIVE.write_text("\n".join(s for _, s in dedup) + "\n", encoding="utf-8")
    mapping = ",".join(f"{user_of(s)}={ip}" for ip, s in dedup)
    lines = [(f"IR_SLOT_EGRESS_IPS={mapping}"
              if ln.startswith("IR_SLOT_EGRESS_IPS") else ln)
             for ln in ENV.read_text(encoding="utf-8").splitlines()]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nusable(POST-CLEAR) {len(good)}/{len(slots)}"
          f" | 去重后 {len(dedup)} 个不同出口 IP -> slots_active.txt",
          flush=True)
    return len(dedup)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
