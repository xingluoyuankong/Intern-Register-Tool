#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读分析：从 loop 日志里按出口 IP / 重试序号统计注册成败。

不修改任何生产文件，纯读取日志。
"""
import glob
import re
from collections import Counter, defaultdict

log = ""
for f in sorted(glob.glob("/root/intern-register/logs/loop*.log")):
    try:
        log += open(f, encoding="utf-8", errors="ignore").read()
    except Exception:
        pass

lines = log.splitlines()
ip_stat = defaultdict(lambda: {"t": 0, "waf": 0, "r429": 0, "ok": 0, "net": 0})
cur_ip = None
for ln in lines:
    m = re.search(r"配额记在 (\d+\.\d+\.\d+\.\d+) 名下", ln)
    if m:
        cur_ip = m.group(1)
        ip_stat[cur_ip]["t"] += 1
        continue
    if cur_ip:
        if "Expecting value" in ln:
            ip_stat[cur_ip]["waf"] += 1
            cur_ip = None
        elif "429 Client Error" in ln:
            ip_stat[cur_ip]["r429"] += 1
            cur_ip = None
        elif "HTTPSConnectionPool" in ln:
            ip_stat[cur_ip]["net"] += 1
            cur_ip = None
        elif "registered: uid=" in ln:
            ip_stat[cur_ip]["ok"] += 1
            cur_ip = None

print("=== 各出口 IP 表现 ===")
print("%-18s%6s%6s%6s%6s%6s%8s" % ("IP", "尝试", "WAF", "429", "NET", "成功", "成功率"))
tot = ok = 0
for ip, s in sorted(ip_stat.items(), key=lambda x: -x[1]["t"]):
    rate = s["ok"] / s["t"] * 100 if s["t"] else 0
    tot += s["t"]
    ok += s["ok"]
    print("%-18s%6d%6d%6d%6d%6d%7.0f%%"
          % (ip, s["t"], s["waf"], s["r429"], s["net"], s["ok"], rate))
if tot:
    print("合计: 尝试 %d, 注册成功 %d, 成功率 %.1f%%" % (tot, ok, ok / tot * 100))

print()
print("=== 尝试序号 vs 结果（每账号最多换 3 个出口）===")
seq = defaultdict(Counter)
cur_seq = None
for ln in lines:
    m = re.search(r"\[第 (\d)/3 次\]", ln)
    if m:
        cur_seq = m.group(1)
        continue
    if cur_seq:
        if "Expecting value" in ln:
            seq[cur_seq]["WAF"] += 1
            cur_seq = None
        elif "429 Client Error" in ln:
            seq[cur_seq]["429"] += 1
            cur_seq = None
        elif "HTTPSConnectionPool" in ln:
            seq[cur_seq]["NET"] += 1
            cur_seq = None
        elif "registered: uid=" in ln:
            seq[cur_seq]["OK"] += 1
            cur_seq = None
for k in sorted(seq):
    print("  第%s次尝试: %s" % (k, dict(seq[k])))

print()
print("=== WAF 挑战与时段（UTC 小时）===")
hc = Counter()
cur_h = None
for ln in lines:
    m = re.search(r"\[round \d+\] exit=\d+\s+(\d{4}-\d{2}-\d{2}) (\d{2}):", ln)
    if m:
        cur_h = m.group(2)
    if cur_h and "Expecting value" in ln:
        hc[cur_h] += 1
for h in sorted(hc):
    print("  %s时: %d 次 WAF 挑战" % (h, hc[h]))
