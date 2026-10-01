"""诊断探针：当前 active 池逐条打真实注册端点，抓**完整响应证据**。

背景（2026-10-01 16:30 用户质问根因）：
    screen 说 CLEAR（200/429 JSON），真注册却 SSL EOF / Expecting value。
    本探针对每条槽位做与注册**完全同构**的请求，并保留：
      - 出口 IP（ipify，走同一 session）
      - HTTP 状态码
      - Content-Type
      - 响应体前 200 字符（挑战页/405 页/业务 JSON 一眼可辨）
    单次尝试、不重试 —— 测的是**原始链路质量**，不是重试后的乐观值。
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, ".")
from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

ROOT = Path(__file__).resolve().parent
ACTIVE = ROOT / ".workbuddy-ai" / "proxypool" / "slots_active.txt"


def probe(slot: str) -> dict:
    tag = str(int(time.time()))[-6:]
    email = f"dx{tag}@outlook.com"
    payload = {
        "username": f"dx{tag}",
        "email": email,
        "password": encrypt_password(email, "Watch!2026x"),
        "source": config.SOURCE,
        "clientId": config.CLIENT_ID,
    }
    out = {"user": slot.split("://", 1)[-1].rsplit("@", 1)[0].split(":", 1)[0][-12:],
           "egress": "-", "status": 0, "ctype": "", "body": "", "err": ""}
    t0 = time.time()
    try:
        sso = SSOClient(proxy=slot, timeout=12)
        sso.prime_session()
        r = sso._post("/register/byEmail", payload)
        out["status"] = r.status_code
        out["ctype"] = r.headers.get("content-type", "")
        out["body"] = r.text[:200].replace("\n", " ")
    except Exception as ex:  # noqa: BLE001
        out["err"] = f"{type(ex).__name__}: {str(ex)[:160]}"
        # 传输层死了就拿不到出口了；单独再试一次 ipify（短超时）
        try:
            import requests
            s = requests.Session()
            s.trust_env = False
            s.proxies = {"http": slot, "https": slot}
            out["egress"] = s.get("https://api.ipify.org?format=json",
                                  timeout=8).json()["ip"]
        except Exception as ex2:  # noqa: BLE001
            out["egress"] = f"dead({type(ex2).__name__})"
        return out
    try:
        out["egress"] = sso.session.get("https://api.ipify.org?format=json",
                                        timeout=8).json()["ip"]
    except Exception:  # noqa: BLE001
        pass
    out["sec"] = round(time.time() - t0, 1)
    return out


def main() -> int:
    slots = [ln.strip() for ln in ACTIVE.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    print(f"probing {len(slots)} active slots ...", flush=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(probe, slots))
    for r in rows:
        print(json.dumps(r, ensure_ascii=False), flush=True)
    ok = sum(1 for r in rows if r["status"] in (200, 429))
    print(f"\nJSON-OK {ok}/{len(slots)}", flush=True)
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
