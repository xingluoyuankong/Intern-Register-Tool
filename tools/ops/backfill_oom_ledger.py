"""把 OOM 丢失的 key 记录补录进服务器台账（batch1 的 2 条 + batch3 的 7 条）。

这些记录有 email/key、没有密码（进程死在写台账前，密码随之丢失）——
password 置 `(lost-oom)` 明示不可再登录，key 本身仍可用。
用项目 ledger.save 落一个新 runs 快照 + 刷新 latest.json，格式与正常批次一致。

🔴 2026-10-16：**不再把 key 硬编码在源码里**。本文件曾在 public 仓库里
   明文躺着两个真实 `sk-…`（泄漏闸门点名）——已改为从**本地** `keys_batch1.json`
   / `keys_batch3.json` 读（两者都在 `.gitignore` 的 `*.json` 家族里，不入库）。
   keys_batch1.json 形状：`[{"email": "...", "api_key": "sk-..."}, ...]`
"""
import json
import os
import sys
import time

sys.path.insert(0, ".")
from src import ledger  # noqa: E402


def _load(name: str) -> list:
    if not os.path.isfile(name):
        print(f"⚠ 缺 {name}（本地文件，不入库）——跳过该批")
        return []
    return json.load(open(name, encoding="utf-8"))


def main() -> None:
    recs = []
    for r in _load("keys_batch1.json"):
        recs.append({"email": r["email"], "api_key": r["api_key"],
                     "status": "ok", "password": "(lost-oom)",
                     "note": "batch1 OOM 补录"})
    for r in _load("keys_batch3.json"):
        recs.append({"email": r["email"], "api_key": r["api_key"],
                     "status": "ok", "password": "(lost-oom)",
                     "note": f"batch3 slot{r.get('slot', '?')} OOM 补录"})

    existing = ledger.load_existing("ledger/latest.json")
    merged = existing + recs
    out = f"ledger/runs/{time.strftime('%Y-%m-%d')}/results-backfill-oom.json"
    ledger.save(out, merged)
    print(f"backfilled {len(recs)} -> {out} (total {len(merged)})")


if __name__ == "__main__":
    main()
