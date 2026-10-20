"""注册情况报告生成器（2026-10-17）——由 cron 任务每小时调用。

用法：
    .venv/bin/python tools/ops/reg_report.py [--hours 1]

输出一份紧凑文本报告（北京时间）：
  - 窗口内新 key 数 + 邮箱域名分布
  - 窗口内注册尝试 成功/失败 + 失败原因 top
  - 出口池健康（最近一次预检 N/40）+ regloop 状态
  - GitHub 推送状态（logs/key_sync.log 最近一行）+ key-sync 状态
  - verdict：正常 / 降速 / 停摆（停摆 = 窗口 0 产出且 regloop 异常）

数据源全部本地（台账 + 日志 + supervisorctl），不打目标站、零请求。
"""

import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def beijing(ts: float = None) -> str:
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    return time.strftime("%H:%M", time.localtime(ts))


def load_records() -> list[dict]:
    snaps = sorted(glob.glob(os.path.join(
        ROOT, "ledger", "runs", "*", "results-*.json")))
    if not snaps:
        return []
    try:
        data = json.load(open(snaps[-1], encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if isinstance(data, dict):
        data = data.get("records") or data.get("data") or []
    return [r for r in data if isinstance(r, dict)]


def window_rows(rows, hours):
    """窗口内（epoch 无时区歧义）：新 key = 有 sk-；失败 = status=failed 且无 key。

    🔴 created_at 是 UTC 字符串，用 `calendar.timegm` 直转 epoch ——
       mktime/tzset 混着用会差 8 小时（第一版就栽在这）。
    """
    import calendar
    cutoff = time.time() - hours * 3600
    out, fails = [], []
    for r in rows:
        ca = r.get("created_at") or ""
        try:
            t = calendar.timegm(time.strptime(ca, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if t < cutoff:
            continue
        has_key = str(r.get("api_key") or "").startswith("sk-")
        if has_key:
            out.append(r)          # 口径与 key_sync.py 对齐：有 key 即算产出
        elif r.get("status") == "failed":
            fails.append(r)
    return out, fails


def fail_reasons(fails) -> list[tuple[str, int]]:
    c = collections.Counter()
    for r in fails:
        err = (r.get("error") or "")[:60]
        if "A0210" in err or "already ex" in err:
            c["用户名撞车"] += 1
        elif "限流" in err or "Too many" in err:
            c["429限流"] += 1
        elif "复查" in err or "gateway" in err.lower() or "Proxy" in err:
            c["出口漂死"] += 1
        elif "aliyun_waf" in err or "html" in err.lower() or "char 0" in err:
            c["WAF滑块"] += 1
        else:
            c[err[:24] or "未知"] += 1
    return c.most_common(3)


def pool_health() -> str:
    """loop.log 里最近一次出口预检的 N/40。"""
    try:
        tail = subprocess.run(
            ["bash", "-lc",
             f"grep -a '出口预检：' {ROOT}/logs/loop.log | tail -1"],
            capture_output=True, text=True, timeout=15).stdout.strip()
        m = re.search(r"(\d+)/(\d+) 个槽位出口可达", tail)
        return f"{m.group(1)}/{m.group(2)}" if m else "?"
    except Exception:
        return "?"


def svc_status(name: str) -> str:
    try:
        out = subprocess.run(
            ["supervisorctl", "-c", "/etc/supervisor-ir.conf", "status", name],
            capture_output=True, text=True, timeout=15).stdout.strip()
        return "RUNNING" if "RUNNING" in out else (out[:30] or "?")
    except Exception:
        return "?"


def last_sync() -> str:
    try:
        lines = open(os.path.join(ROOT, "logs", "key_sync.log"),
                     encoding="utf-8").read().strip().splitlines()
        return lines[-1] if lines else "（还没跑过）"
    except OSError:
        return "（无日志）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1)
    args = ap.parse_args()

    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    now = time.strftime("%m-%d %H:%M")

    rows = load_records()
    # 口径与 key_sync.py 完全一致：有 sk- key 即计入（status 有历史多态，别筛）
    total = sum(1 for r in rows
                if str(r.get("api_key") or "").startswith("sk-"))
    window, fails = window_rows(rows, args.hours)

    dom = collections.Counter(
        (r.get("email") or "?").split("@")[-1] for r in window)
    dom_s = " ".join(f"{d}×{n}" for d, n in dom.most_common()) or "—"

    fr = fail_reasons(fails)
    fr_s = "，".join(f"{k}×{v}" for k, v in fr) or "无"

    regloop = svc_status("regloop")
    keysync = svc_status("key-sync")
    per_hour = len(window) / max(args.hours, 1)

    # verdict：regloop 挂 = 停摆；窗口 0 产出 = 停滞；<2/h = 降速；否则正常
    if regloop != "RUNNING":
        verdict = f"🔴 停摆：regloop={regloop}（supervisorctl -c /etc/supervisor-ir.conf status 查）"
    elif not window:
        verdict = "⚠️ 停滞：窗口内 0 产出 —— 查 loop.log 末尾（域名烧了弹滑块？出口全死？）"
    elif per_hour < 2:
        verdict = "🟡 降速：低于设计节奏 12/h —— 多半是出口漂死/撞车损耗"
    else:
        verdict = "🟢 正常"

    print(f"📊 注册报告（北京 {now}，窗口近 {args.hours}h）")
    print(f"✅ 新 key：{len(window)} 个（累计 {total}）｜域名：{dom_s}")
    print(f"⚙️ 尝试 {len(window) + len(fails)}，成功 {len(window)}，失败 {len(fails)}（{fr_s}）")
    print(f"🔀 出口池：最近预检 {pool_health()} 活｜regloop={regloop}")
    print(f"📤 GitHub：{last_sync()}｜key-sync={keysync}")
    print(f"结论：{verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
