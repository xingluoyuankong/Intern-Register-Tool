#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：对比"优化前 vs 优化后"的注册成功率（从 loop 日志解析）。

优化前 = 每轮都从 slot1 开始（进程内 _uses 归零）
优化后 = 槽位计数持久化 + 按订阅链接记账
"""
import glob
import re
from collections import Counter

log = ""
for f in sorted(glob.glob("/root/intern-register/logs/loop*.log")):
    try:
        log += open(f, encoding="utf-8", errors="ignore").read()
    except Exception:
        pass

lines = log.splitlines()

# 以"20 槽位上线"为新代理起点，再以"轮转修复上线"为分界
pivot = 0
for i, ln in enumerate(lines):
    # 轮转修复后首次出现非 slot1 起始；用计数文件首次写入不好判断，
    # 这里用"槽位使用分布铺开"——简化：按 ROUND 时间戳切（06:30 之后）
    pass

# 用"注册尝试"与"注册成功"计数，按时间分桶
def bucket(ts):
    """ts 形如 '2026-10-03 20:28:19'"""
    return ts

cur_ts = ""
stats = {"before": Counter(), "after": Counter()}
for ln in lines:
    m = re.search(r"\[round \d+\] exit=\d+\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ln)
    if m:
        cur_ts = m.group(1)
    if not cur_ts:
        continue
    # 分界：优化上线时间 2026-10-04 05:35（轮转）
    key = "after" if cur_ts >= "2026-10-04 05:35" else "before"
    if "registered: uid=" in ln:
        stats[key]["reg_ok"] += 1
    elif "Expecting value" in ln:
        stats[key]["waf"] += 1
    elif "429 Client Error" in ln:
        stats[key]["r429"] += 1
    elif "HTTPSConnectionPool" in ln or "ConnectionPool" in ln:
        stats[key]["net"] += 1
    elif "quota guard" in ln or "配额" in ln:
        stats[key]["quota"] += 1
    if "DONE: 1/1 succeeded" in ln:
        stats[key]["key_ok"] += 1
    elif "DONE: 0/1 succeeded" in ln:
        stats[key]["key_fail"] += 1

for k in ("before", "after"):
    s = stats[k]
    att = s["reg_ok"] + s["waf"] + s["r429"] + s["net"] + s["quota"]
    rate = s["reg_ok"] / att * 100 if att else 0
    print(f"[{k}] 注册尝试 {att}  成功 {s['reg_ok']}  成功率 {rate:.1f}%")
    print(f"      失败构成: WAF {s['waf']} / 429 {s['r429']} / 网络 {s['net']} / 配额 {s['quota']}")
    print(f"      出 key: 成功 {s['key_ok']}  失败 {s['key_fail']}")
