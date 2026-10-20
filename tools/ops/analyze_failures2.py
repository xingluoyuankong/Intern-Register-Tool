#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读分析 v2：只统计新代理上线后的数据，按出口 IP 算真实成功率。

新代理上线时间点 = 2026-10-03 19:34（服务器 loop.log 首轮新槽位）。
"""
import glob
import re
from collections import Counter, defaultdict

CUT = "2026-10-03 19:30"

log = ""
for f in sorted(glob.glob("/root/intern-register/logs/loop*.log")):
    try:
        log += open(f, encoding="utf-8", errors="ignore").read()
    except Exception:
        pass

lines = log.splitlines()

# 只保留切换点之后的行
idx = 0
for i, ln in enumerate(lines):
    if ln.startswith("########## ROUND") and CUT <= "2026-10-03 19:30":
        pass
# 简化：找到最后一个 "槽位代理池已启用" 之前带 20 个槽位的那轮起点
start = 0
for i, ln in enumerate(lines):
    if "20 个槽位" in ln or "槽位 20 个" in ln:
        start = i
        break
sub = lines[start:]
print("分析起点行 %d（新代理 20 槽位上线处），样本行数 %d" % (start, len(sub)))

ip_stat = defaultdict(lambda: {"t": 0, "waf": 0, "r429": 0, "net": 0, "ok": 0})
slot_stat = defaultdict(lambda: {"t": 0, "ok": 0})
cur_ip = None
cur_slot = None
for ln in sub:
    m = re.search(r"出口槽位 (slot\d+)\(", ln)
    if m:
        cur_slot = m.group(1)
        slot_stat[cur_slot]["t"] += 1
    m2 = re.search(r"配额记在 (\d+\.\d+\.\d+\.\d+) 名下", ln)
    if m2:
        cur_ip = m2.group(1)
        ip_stat[cur_ip]["t"] += 1
        continue
    if cur_ip:
        if "Expecting value" in ln:
            ip_stat[cur_ip]["waf"] += 1
            cur_ip = None
        elif "429 Client Error" in ln:
            ip_stat[cur_ip]["r429"] += 1
            cur_ip = None
        elif "HTTPSConnectionPool" in ln or "Timeout" in ln:
            ip_stat[cur_ip]["net"] += 1
            cur_ip = None
        elif "registered: uid=" in ln:
            ip_stat[cur_ip]["ok"] += 1
            if cur_slot:
                slot_stat[cur_slot]["ok"] += 1
            cur_ip = None

print()
print("=== 新代理各出口 IP 真实表现 ===")
print("%-18s%6s%6s%6s%6s%6s%8s" % ("IP", "尝试", "WAF", "429", "NET", "成功", "成功率"))
tot = ok = 0
rows = []
for ip, s in sorted(ip_stat.items(), key=lambda x: -x[1]["ok"]):
    rate = s["ok"] / s["t"] * 100 if s["t"] else 0
    tot += s["t"]
    ok += s["ok"]
    rows.append((ip, s, rate))
for ip, s, rate in rows:
    print("%-18s%6d%6d%6d%6d%6d%7.0f%%"
          % (ip, s["t"], s["waf"], s["r429"], s["net"], s["ok"], rate))
if tot:
    print("合计: 尝试 %d, 成功 %d, 成功率 %.1f%%" % (tot, ok, ok / tot * 100))

print()
print("=== 各槽位（slot）表现 ===")
for sl, s in sorted(slot_stat.items(), key=lambda x: -x[1]["t"]):
    print("  %-8s 尝试 %3d  成功 %2d  %s"
          % (sl, s["t"], s["ok"],
             ("%.0f%%" % (s["ok"] / s["t"] * 100)) if s["t"] else "-"))
