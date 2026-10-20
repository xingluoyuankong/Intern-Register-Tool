#!/usr/bin/env python3
"""并行重探槽位池 → 生成 slots_active.txt（比 refresh_slots.py 快 N 倍）。

refresh_slots.py 是**串行**探测（150 槽 × 15s ≈ 37 分钟），线上等不起。
这里用线程池并发探（默认 30 并发），150 槽约 1 分钟。

判据与 refresh_slots.py 完全一致：
  1. 经代理拿出口 IP（ipify）
  2. GET sso.openxlab.org.cn/register 能通（TLS 握手 + <500）
两者都过才算可达，写进 slots_active.txt。
"""
import concurrent.futures
import sys
from pathlib import Path

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
POOL = ROOT / ".workbuddy-ai" / "proxypool" / "slots.txt"
ACTIVE = ROOT / ".workbuddy-ai" / "proxypool" / "slots_active.txt"
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 30


def user_of(slot: str) -> str:
    return slot.split("://", 1)[-1].rsplit("@", 1)[0].split(":", 1)[0]


def probe(slot: str) -> tuple:
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": slot, "https": slot}
    try:
        ip = s.get("https://api.ipify.org?format=json", timeout=15).json()["ip"]
    except Exception:
        return ("", "")
    try:
        r = s.get(f"{config.SSO_BASE}/register", timeout=15)
        if r.status_code >= 500:
            return ("", "")
    except Exception:
        return ("", "")
    return (slot, ip)


def main() -> int:
    slots = [ln.strip() for ln in POOL.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    print(f"池共 {len(slots)} 槽，{WORKERS} 并发探测…", flush=True)
    pairs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for slot, ip in ex.map(probe, slots):
            ok = bool(ip)
            print(f"{'OK ' if ok else 'BAD'} {user_of(slot)[:18]:<20} {ip}", flush=True)
            if ok:
                pairs.append(slot)
    ACTIVE.write_text("\n".join(pairs) + "\n", encoding="utf-8")
    print(f"usable {len(pairs)}/{len(slots)} -> {ACTIVE}", flush=True)
    return len(pairs)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
