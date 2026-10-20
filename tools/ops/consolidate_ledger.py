#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台账合并 + 冗余快照清理（治磁盘泄漏）。

背景
----
`run.py` 每轮都往 `ledger/runs/<日期>/results-<时间戳>.json` 写一份**全量合并**
快照。单轮注册是无限循环 → 一天几千份，每份 2.3MB → 实测堆积 **4293 份 / 4.9GB**。
更麻烦的是：最新那份快照**并不总是**包含全部记录（实测最新 39 个 key，
全量合并有 48 个）→ 所以**不能直接删旧**，必须先合并出一份权威快照。

本脚本做的事
------------
1. 扫描全部快照，按 email 合并（**有 api_key 的记录优先**，其次取 created_at 较新的）
   —— 保证不会丢任何已生成的 key；
2. 写出一份权威快照到 `ledger/runs/<今天>/results-<时间戳>.json`；
3. **校验**新快照确实覆盖全部带 key 账号后，才删除其余冗余快照；
4. 打印前后体积/条数对比。

用法：
    python tools/ops/consolidate_ledger.py            # 合并 + 清理
    python tools/ops/consolidate_ledger.py --dry-run  # 只算不写
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNS = os.path.join(ROOT, "ledger", "runs")


def _size(path: str) -> int:
    total = 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


def load_all() -> dict:
    """合并全部快照 → {email: record}。有 key 优先，其次取较新。"""
    seen: dict = {}
    files = sorted(glob.glob(os.path.join(RUNS, "*", "*.json")))
    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for r in (d if isinstance(d, list) else [d]):
            if not isinstance(r, dict):
                continue
            em = (r.get("email") or "").strip()
            if not em:
                continue
            old = seen.get(em)
            if old is None:
                seen[em] = dict(r)
                continue
            old_key = bool(old.get("api_key"))
            new_key = bool(r.get("api_key"))
            if new_key and not old_key:
                seen[em] = dict(r)                       # 有 key 的优先
            elif old_key == new_key:
                if (r.get("created_at") or "") >= (old.get("created_at") or ""):
                    seen[em] = dict(r)                   # 同状态取较新
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写不删")
    ap.add_argument("--keep", type=int, default=1,
                    help="每天保留的最新快照份数（默认 1：只留权威那份）")
    args = ap.parse_args()

    before = _size(RUNS)
    files = sorted(glob.glob(os.path.join(RUNS, "*", "*.json")))
    merged = load_all()
    keys = [r for r in merged.values() if r.get("api_key")]

    print(f"扫描 {len(files)} 份快照 | 合并后账号 {len(merged)} 个 | 带 key {len(keys)} 个")
    print(f"当前台账体积 {before / 1024 / 1024:.0f} MB")

    if args.dry_run:
        print("--dry-run：不写不删")
        return 0

    if not merged:
        print("没有可合并的记录，退出")
        return 1

    # ① 写权威快照（今天日期目录下）
    today = time.strftime("%Y-%m-%d")
    outdir = os.path.join(RUNS, today)
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"results-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(list(merged.values()), open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ② 校验：新快照必须覆盖全部带 key 账号，否则回滚退出（绝不删旧）
    check = json.load(open(out, encoding="utf-8"))
    ck = {r["email"] for r in check if isinstance(r, dict) and r.get("api_key")}
    want = {r["email"] for r in keys}
    if not (want <= ck):
        print(f"❌ 校验失败：新快照少了 {len(want - ck)} 个带 key 账号，已回滚不删旧")
        os.remove(out)
        return 1
    print(f"✅ 权威快照已写入并校验通过：{os.path.basename(out)}（{len(check)} 条）")

    # ③ 删除其余冗余快照（按天保留最新 keep 份，且保留刚写的这份）
    removed = 0
    by_day: dict = {}
    for f in files:
        if os.path.abspath(f) == os.path.abspath(out):
            continue
        by_day.setdefault(os.path.dirname(f), []).append(f)
    for day, fs in by_day.items():
        fs.sort(key=os.path.getmtime, reverse=True)
        for f in fs[args.keep - 1:] if args.keep > 0 else fs:
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
        # 空目录清掉
        try:
            if not os.listdir(day):
                os.rmdir(day)
        except OSError:
            pass

    after = _size(RUNS)
    print(f"已删除冗余快照 {removed} 份")
    print(f"台账体积 {before / 1024 / 1024:.0f} MB → {after / 1024 / 1024:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
