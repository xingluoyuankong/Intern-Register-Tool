#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
只读分析脚本（不改 src/）：
1) 按收件域名统计历史激活情况（stages.activate=='ok'、邮件超时、注册 ok 但未激活）
2) 今天(2026-10-19)注册账号的域名分布与结果
3) loop.log 今天按小时的 activated / 激活邮件超时 分布

数据源：ledger/export_all.json + ledger/runs/*/results-*.json + logs/loop.log
"""
import json
import glob
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TODAY = "2026-10-19"


def load_records():
    """按 email 合并所有 run 快照（后出现的覆盖先出现的，即最新状态）。"""
    recs = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "ledger/runs/*/results-*.json"))):
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            print(f"[warn] 读取失败 {f}: {e}", file=sys.stderr)
            continue
        if not isinstance(data, list):
            continue
        for r in data:
            if isinstance(r, dict) and r.get("email"):
                recs[r["email"]] = r
    # export_all 作为补充（较旧，只在 runs 里没有时采用）
    exp = os.path.join(ROOT, "ledger/export_all.json")
    added = 0
    if os.path.exists(exp):
        for r in json.load(open(exp, encoding="utf-8")):
            if isinstance(r, dict) and r.get("email") and r["email"] not in recs:
                recs[r["email"]] = r
                added += 1
    return recs, added


def dom(email):
    if not email or "@" not in email:
        return "<invalid>"
    return email.rsplit("@", 1)[1].strip().lower()


def classify(r):
    st = r.get("stages") or {}
    act = st.get("activate")
    reg = st.get("register")
    err = (r.get("error") or "").lower()
    timed_out = ("激活邮件超时" in (r.get("error") or "")) or (act == "timeout")
    return {
        "register_ok": reg == "ok",
        "activate": act,
        "activated": act == "ok",
        "mail_timeout": timed_out,
        "status": r.get("status"),
        "error_kind": r.get("error_kind"),
        "error": r.get("error") or "",
    }


def main():
    recs, added = load_records()
    print(f"# 合并后唯一账号: {len(recs)}（export_all 补入 {added} 条）")
    all_ts = [r.get("created_at", "") for r in recs.values() if r.get("created_at")]
    print(f"# created_at 区间: {min(all_ts)} ~ {max(all_ts)}")

    # ---------- 1. 按域名 ----------
    per = defaultdict(lambda: {
        "n": 0, "activated": 0, "mail_timeout": 0, "register_ok": 0,
        "act_times": [], "first": None, "last": None,
    })
    for email, r in recs.items():
        d = dom(email)
        c = classify(r)
        p = per[d]
        p["n"] += 1
        ts = r.get("created_at") or ""
        if ts:
            p["first"] = ts if p["first"] is None else min(p["first"], ts)
            p["last"] = ts if p["last"] is None else max(p["last"], ts)
        if c["register_ok"]:
            p["register_ok"] += 1
        if c["activated"]:
            p["activated"] += 1
            p["act_times"].append(ts)
        if c["mail_timeout"]:
            p["mail_timeout"] += 1

    rows = sorted(per.items(), key=lambda kv: (-kv[1]["activated"], -kv[1]["n"]))
    print("\n## 表1  按收件域名历史激活统计（全量台账）")
    print(f"{'domain':<26}{'账号数':>6}{'注册ok':>7}{'激活ok':>7}{'邮件超时':>9}{'激活率':>8}  最后成功激活时间")
    print("-" * 100)
    for d, p in rows:
        rate = f"{p['activated']/p['register_ok']*100:.1f}%" if p["register_ok"] else "-"
        last_ok = max(p["act_times"]) if p["act_times"] else "—"
        print(f"{d:<26}{p['n']:>6}{p['register_ok']:>7}{p['activated']:>7}{p['mail_timeout']:>9}{rate:>8}  {last_ok}")

    # ---------- 2. 今天 ----------
    today = {e: r for e, r in recs.items() if (r.get("created_at") or "").startswith(TODAY)}
    print(f"\n## 表2  今日({TODAY})注册明细  n={len(today)}")
    print(f"{'created_at':<20}{'domain':<22}{'email':<40}{'status':<9}{'activate':<10}{'error_kind':<10}")
    print("-" * 118)
    for e, r in sorted(today.items(), key=lambda kv: kv[1].get("created_at") or ""):
        c = classify(r)
        act = c["activate"] or ("超时" if c["mail_timeout"] else "-")
        print(f"{(r.get('created_at') or ''):<20}{dom(e):<22}{e:<40}{(c['status'] or ''):<9}{act:<10}{(c['error_kind'] or '-'):<10}")

    tper = defaultdict(lambda: {"n": 0, "activated": 0, "mail_timeout": 0, "register_ok": 0, "other_fail": 0})
    for e, r in today.items():
        c = classify(r)
        p = tper[dom(e)]
        p["n"] += 1
        p["activated"] += c["activated"]
        p["mail_timeout"] += c["mail_timeout"]
        if c["register_ok"]:
            p["register_ok"] += 1
        elif not c["mail_timeout"]:
            p["other_fail"] += 1
    print(f"\n## 表3  今日按域名汇总")
    print(f"{'domain':<22}{'账号数':>6}{'注册ok':>7}{'激活ok':>7}{'邮件超时':>9}{'注册前失败':>10}")
    print("-" * 70)
    for d, p in sorted(tper.items(), key=lambda kv: -kv[1]["n"]):
        print(f"{d:<22}{p['n']:>6}{p['register_ok']:>7}{p['activated']:>7}{p['mail_timeout']:>9}{p['other_fail']:>10}")

    # 今日激活/超时的时间分布（账号级）
    print(f"\n## 表4  今日账号级结果·按小时")
    byhour = defaultdict(lambda: Counter())
    for e, r in today.items():
        c = classify(r)
        h = (r.get("created_at") or "")[11:13]
        byhour[h]["total"] += 1
        byhour[h]["activated"] += c["activated"]
        byhour[h]["mail_timeout"] += c["mail_timeout"]
    print(f"{'hour':<6}{'账号':>5}{'激活ok':>8}{'邮件超时':>9}")
    for h in sorted(byhour):
        v = byhour[h]
        print(f"{h:<6}{v['total']:>5}{v['activated']:>8}{v['mail_timeout']:>9}")

    # ---------- 3. loop.log ----------
    logp = os.path.join(ROOT, "logs/loop.log")
    print(f"\n## 表5  logs/loop.log 事件行时间分布（按小时，全部日期）")
    ev_re = re.compile(r"activated|激活邮件超时")
    hour_re = re.compile(r"(20\d\d-\d\d-\d\d)[ T](\d\d):(\d\d):(\d\d)")
    cur_day, cur_hour = None, None
    day_hour = defaultdict(lambda: Counter())
    today_hour = defaultdict(lambda: Counter())
    with open(logp, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = hour_re.search(line)
            if m:
                cur_day, cur_hour = m.group(1), m.group(2)
            if ev_re.search(line):
                key = cur_day or "<unknown>"
                if "激活邮件超时" in line:
                    day_hour[(cur_day, cur_hour)]["mail_timeout"] += 1
                    if cur_day == TODAY:
                        today_hour[cur_hour]["mail_timeout"] += 1
                else:
                    day_hour[(cur_day, cur_hour)]["activated"] += 1
                    if cur_day == TODAY:
                        today_hour[cur_hour]["activated"] += 1

    days = sorted({d for d, _ in day_hour if d})
    print("按日汇总：")
    print(f"{'date':<14}{'activated行':>12}{'激活邮件超时行':>16}")
    for d in days:
        a = sum(v["activated"] for (dd, h), v in day_hour.items() if dd == d)
        t = sum(v["mail_timeout"] for (dd, h), v in day_hour.items() if dd == d)
        print(f"{d:<14}{a:>12}{t:>16}")

    print(f"\n## 表6  今日({TODAY}) loop.log 按小时")
    print(f"{'hour':<6}{'activated行':>12}{'激活邮件超时行':>16}")
    for h in sorted(x for x in today_hour if x):
        v = today_hour[h]
        print(f"{h:<6}{v['activated']:>12}{v['mail_timeout']:>16}")

    print(f"\n## 表7  历史各日 activated/超时 按小时（用于判定时间窗口）")
    print(f"{'date':<12}{'hour':<6}{'activated':>10}{'timeout':>9}")
    for (d, h) in sorted(day_hour, key=lambda k: (k[0] or "", k[1] or "")):
        v = day_hour[(d, h)]
        print(f"{d:<12}{h:<6}{v['activated']:>10}{v['mail_timeout']:>9}")


if __name__ == "__main__":
    main()
