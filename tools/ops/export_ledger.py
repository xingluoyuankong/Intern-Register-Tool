#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把**全量合并台账**导出为 `ledger/export_all.json`，供本地同步拉取。

🔴 为什么不用 latest.json：
    `src/ledger.py` 明确写了 —— `latest.json` 只是"**本批结果**"
    （只含最近一次落盘写进去的记录，1 条也是正常的），**不能当读源**。
    全量 = `ledger/runs/**/*.json` 按 email 合并（`ledger.load_existing`）。
    2026-10-14 实测：latest.json 只有 1 条，全量有 1887 条 / 456 key ——
    本地同步脚本拉 latest.json 导致本地永远落后服务器。

用法（服务器上）：.venv/bin/python tools/ops/export_ledger.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def _glob_merge() -> list:
    """按 runs/*/*.json 全量 glob 合并（与 dashboard.load_records 同口径）。

    🔴 2026-10-16 实测：`ledger.load_existing(ledger.ledger_path())` 只读到
    1889 条 / 457 key，而 glob 合并是 **1982 条 / 533 key** —— ledger_path()
    的口径更窄，会把一部分账号漏掉。导出必须以 glob 为准，否则本地同步
    永远少于面板。
    """
    import glob as _g
    recs = {}
    for f in sorted(_g.glob(os.path.join(ROOT, "ledger", "runs", "*", "*.json"))):
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
            old = recs.get(em)
            if old is None:
                recs[em] = dict(r)
            else:
                # 有 key 的优先；同状态取较新
                if bool(r.get("api_key")) and not old.get("api_key"):
                    recs[em] = dict(r)
                elif bool(r.get("api_key")) == bool(old.get("api_key")):
                    if (r.get("created_at") or "") >= (old.get("created_at") or ""):
                        recs[em] = dict(r)
    return list(recs.values())


recs = _glob_merge()
keys = sum(1 for r in recs if (r.get("api_key") or "").strip())

out = os.path.join(ROOT, "ledger", "export_all.json")
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(recs, f, ensure_ascii=False)
os.replace(tmp, out)
print(f"EXPORT_OK records={len(recs)} keys={keys} -> {out}")
