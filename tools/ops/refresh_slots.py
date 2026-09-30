"""刷新可用槽位：目标站级探测 → 写 slots_active.txt + 更新 .env 映射。

Resin 是 15 分钟窗口轮换出口，节点池里"能通国内目标站"的比例随时间波动
（实测同一批 10 个用户名：好的时候 10/10，差的时候 3/10 —— 差别在出口节点
到 sso.openxlab.org.cn 的回程链路，不是代理服务本身）。

所以每轮批量前必须重探：
  1. 拿出口 IP（ipify）
  2. GET/POST 探目标站（TLS 能握手 + 有响应）
  通过者写进 slots_active.txt，并同步 IR_SLOT_EGRESS_IPS（键=用户名，
  值=该槽位当前出口 IP —— 配额按会话桶记账）。
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]      # tools/ops/x.py -> 项目根
SLOTS = ROOT / ".workbuddy-ai" / "proxypool" / "slots.txt"
ACTIVE = ROOT / ".workbuddy-ai" / "proxypool" / "slots_active.txt"
ENV = ROOT / ".env"


def user_of(slot: str) -> str:
    s = slot.split("://", 1)[-1].rsplit("@", 1)[0]
    return s.split(":", 1)[0]


def probe(slot: str, timeout: int = 15) -> str:
    """返回出口 IP；不通返回空串。"""
    s = requests.Session()
    s.proxies = {"http": slot, "https": slot}
    try:
        ip = s.get("https://api.ipify.org?format=json",
                   timeout=timeout).json()["ip"]
    except Exception:  # noqa: BLE001
        return ""
    try:
        r = s.get(f"{config.SSO_BASE}/register", timeout=timeout)
        if r.status_code >= 500:
            return ""
    except Exception:  # noqa: BLE001 SSL RST / 超时都算不通
        return ""
    return ip


def main() -> int:
    slots = [ln.strip() for ln in SLOTS.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    pairs = []
    for s in slots:
        ip = probe(s)
        print(f"{'OK ' if ip else 'BAD'} {user_of(s)[:16]:<18} {ip}", flush=True)
        if ip:
            pairs.append((user_of(s), ip, s))

    ACTIVE.write_text("\n".join(p[2] for p in pairs) + "\n", encoding="utf-8")
    mapping = ",".join(f"{u}={ip}" for u, ip, _ in pairs)
    lines = []
    for ln in ENV.read_text(encoding="utf-8").splitlines():
        if ln.startswith("IR_SLOT_EGRESS_IPS"):
            lines.append(f"IR_SLOT_EGRESS_IPS={mapping}")
        else:
            lines.append(ln)
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"usable {len(pairs)}/{len(slots)} -> slots_active.txt", flush=True)
    return len(pairs)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
