#!/usr/bin/env python
r"""从账号台账里剪掉**没有账号信息的空记录**（配额守卫中止的残渣）。

为什么需要它
------------
台账（`ledger/runs/<日期>/results-<时间戳>.json`，读源 = 最新那份全量快照）同时是
"运行报告"和"账号台账"。配额守卫主动中止的账号
（`status="skipped"`，`error` 形如 `quota guard: ... 未发请求`）**会被写进台账**，
但它们**一个字段都没有** —— email / username / password / jwt / api_key
全是空串，`stages` 是 `{}`。

2026-09-20 实测：台账 377 条里 **110 条**是这种残渣（09-19 留 60 条，
09-20 那次 50 批次又灌 50 条）。后果：

  * 台账条数（377）与实际账号数（267）**对不上**，报"我们有多少账号"时会多报 41%；
  * 它们是**空的**，连按 email 去重都做不到 —— `merge_records` 拿不到键；
  * 每次跑批撞一次配额守卫就再灌一批，是**累积性**污染，不是一次性的。

🔴 为什么不用 `tools/data/restore_results.py`
-------------------------------------------
那个工具的判据是 `is_account()`，为"**从散落碎片重建**台账"设计 ——
它问的是"这条记录能不能还原出一个可用账号"。用它来修剪报告会**过度删除**：
本次实测它会连**2 条带 email 的网络失败记录**一起丢掉
（`oai-40d0…` 的 `Connection aborted 10053`、`oai-a2fc…` 的 `SSLError`），
而那两条是"50 批次 2 失败"的**唯一书面证据**。删掉等于把失败痕迹抹平，
下次复盘时只剩 48 个成功、看不出跑过 50 个。

所以本工具的判据更窄，且**刻意保留**带 email 的失败记录：

    剪掉：没有任何凭据字段、且 status != success 的记录
    保留：任何带 email / username / password / jwt / api_key / key_id 的记录
    保留：任何 status == "success" 的记录（硬保，不看字段）

⚠ 判据里的 `status != "success"` 不是多余的：一条 success 记录即使字段全空
（理论上不该出现）也**绝不能**剪 —— 那是在删真账号，宁可留着让人看见。

备份与回滚
----------
`--apply` 会先写一份**同目录**备份 `<目标>.bak-prune-<时间戳>`。

目标在不在**台账目录**（`ledger/`）里决定之后写去哪：

  * 在 ⇒ 走 `ledger.save_snapshot()`：剪完的全量落一份
    `ledger/runs/<日期>/results-<时间戳>.json` 快照，**它同时成为新读源**
    （`ledger.ledger_path()` 指向最新快照，不是 `latest.json`）；
  * 不在（`--results x.json` 那种导出用法）⇒ 只写那一个文件。

⚠ 快照与备份**不是一回事**：快照是"这次剪**之后**"的状态，备份是"剪**之前**"
的状态 —— 排查"到底剪掉了什么"时两个都要看。

⚠ 台账目录分支**不动**原文件：原快照还在原地，只是不再是"最新"。所以回滚是
**删掉刚生成的那份新快照**（读源自动退回上一份），而不是往哪拷回去。

🔴 备份名**必须**落在 `.bak*` 家族里。本项目 `.gitignore` 只按家族写
（`*.bak*` / `*.old` / `*.save*` / `*.tmp*` …），**不覆盖 `.pre-*`**
（`migrate_quota_scope.py` 用的是 `.pre-scope-migrate-<ts>`，那是因为它的目标
在 `.workbuddy-ai/state/` 整个目录都被忽略）。台账目录里是**明文凭据**，
照抄那个命名会让一份未忽略的备份被泄漏闸门拦下。

回滚（默认目标）：删掉打印出来的那份新快照即可；要留证据就先看一眼它和 `.bak-prune-*`。

用法
----
    python tools/data/prune_ledger.py                 # 干跑，只报告（默认）
    python tools/data/prune_ledger.py --apply         # 真剪（自动备份）
    python tools/data/prune_ledger.py --results x.json --apply
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import sys
import time
from pathlib import Path

from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

from src import ledger  # noqa: E402

DEFAULT_RESULTS = ledger.ledger_path()

# 任何一项非空 = 这条记录**认领了一个身份**，不能剪。
# 刻意把 `email` 放进来：它同时是 merge 的键，也是"我们真的走到建号那一步了"的证据。
CRED_FIELDS = ("email", "username", "password", "sso_uid", "jwt",
               "api_key", "key_id")


def is_noise(rec: dict) -> bool:
    """这条记录是不是"没有任何账号信息的中止残渣"。"""
    if rec.get("status") == "success":
        # 硬保：success 就算字段全空也不剪（那是在删真账号）。
        return False
    return not any(rec.get(k) for k in CRED_FIELDS)


def _err_head(rec: dict) -> str:
    """取 error 的前 46 字符做归类键（配额守卫文案前缀够用）。"""
    return (rec.get("error") or "(无 error)")[:46]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="剪掉台账里没有账号信息的空记录（配额守卫中止的残渣）")
    ap.add_argument("--results", default=str(DEFAULT_RESULTS),
                    help=f"台账路径（默认 {DEFAULT_RESULTS}）")
    ap.add_argument("--apply", action="store_true",
                    help="真剪（默认只报告）。会自动备份原台账。")
    args = ap.parse_args()

    path = Path(args.results)
    if not path.is_file():
        print(f"✗ 读不到台账：{path}", file=sys.stderr)
        return 1

    recs = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(recs, list):
        print(f"✗ 台账顶层不是 list：{type(recs).__name__}", file=sys.stderr)
        return 1

    dropped = [r for r in recs if is_noise(r)]
    kept = [r for r in recs if not is_noise(r)]

    print(f"台账 {path}")
    print(f"  原有        {len(recs):>4} 条")
    print(f"  待剪        {len(dropped):>4} 条  （无任何凭据字段且非 success）")
    print(f"  保留        {len(kept):>4} 条")

    if dropped:
        print("\n待剪记录按 error 归类：")
        for k, n in collections.Counter(_err_head(r) for r in dropped).most_common():
            print(f"  {n:>4}  {k!r}")
        by_day = collections.Counter((r.get("created_at") or "?")[:10] for r in dropped)
        print("\n待剪记录按日期：")
        for k, n in sorted(by_day.items()):
            print(f"  {n:>4}  {k}")

    # 🔴 对照：**带 email 的失败记录必须留下**。这条打印是给未来的人看的 ——
    #    提醒他别把判据放宽到"失败就剪"，那会抹掉批次的失败痕迹。
    failed_kept = [r for r in kept
                   if r.get("status") == "failed" and not r.get("api_key")]
    if failed_kept:
        print(f"\nℹ 保留 {len(failed_kept)} 条**带 email 的失败**记录"
              f"（它们是「这批跑过但失败」的证据，不要剪）：")
        for r in failed_kept:
            print(f"    {r.get('email')}  {_err_head(r)}")

    if not dropped:
        print("\n✓ 没有可剪的记录，台账未改动。")
        return 0

    if not args.apply:
        print("\n（这是 dry-run，台账未改动。要落地加 --apply）")
        return 0

    # ── 落盘（先备份，再原子替换）──────────────────────────────────
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(path.name + f".bak-prune-{ts}")
    shutil.copy2(path, bak)

    # `existing=[]` 是"防静默缩水"护栏的**官方逃生口** —— 缩水在这里是本次
    # 操作的**目的**，所以显式声明；护栏本身对非预期缩水仍然有效。
    # 🔴 目标在不在**台账目录**里决定落点：
    #    在 → 走 `save_snapshot()`，剪完的全量留一份日期/时间戳快照（并成为新读源）；
    #    不在 → 只写用户给的那个文件（`--results x.json` 的导出用法）。
    # 两条路都走 `ledger` 的落盘函数而不是自己 json.dump：序列化格式
    # （indent=2 / ensure_ascii=False）与原子写只有一处实现，别在这里分叉。
    if ledger.is_ledger_path(path):
        snap, last = ledger.save_snapshot(kept, existing=[])
        dest = snap
        print(f"\n✅ 已写入 {snap}（{len(recs)} → {len(kept)} 条）")
        print(f"   本批结果：{last}")
        print(f"   备份：{bak}")
        # 回滚 = 删掉刚生成的快照；读源（`ledger_path()` = 最新快照）会自动
        # 退回上一份，也就是 `path` 本身（它没被改动过）。
        print(f"   回滚：删除 {snap.name}（读源会自动退回上一份快照）")
    else:
        tmp = path.with_name(path.name + ".tmp")
        ledger.save(tmp, kept, existing=[])
        os.replace(tmp, path)
        dest = path
        print(f"\n✅ 已写入 {dest}（{len(recs)} → {len(kept)} 条）")
        print(f"   备份：{bak}")
        print(f"   回滚：cp {bak} {path}")

    # 回读校验：条数对得上、每条都能解析、没剪掉带凭据的
    back = json.loads(dest.read_text(encoding="utf-8"))
    assert len(back) == len(kept), f"条数对不上！{len(kept)} -> {len(back)}"
    assert not any(is_noise(r) for r in back), "回读后仍有可剪记录"
    print(f"   回读校验：{len(back)} 条，无残渣 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
