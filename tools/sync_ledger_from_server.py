# -*- coding: utf-8 -*-
"""把服务器台账合并进本地（并集，按 email 去重，字段级补齐）。

- 本地已有字段不动，服务器多的字段补上（如新拿到的 api_key）。
- 服务器记录胜出条件：本地该条没 key 而服务器有 → 用服务器的。
- 输出：本地 latest.json 更新 + 一份纯 key 清单 exports/keys_sync.txt。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = os.path.join(ROOT, "ledger", "latest.json")
SERVER = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\XZXyuan\AppData\Local\Temp\srv_latest.json"

local = json.load(open(LOCAL, encoding="utf-8"))
server = json.load(open(SERVER, encoding="utf-8"))

lmap = {d.get("email", ""): d for d in local if d.get("email")}
smap = {d.get("email", ""): d for d in server if d.get("email")}

added = 0
keyed_new = 0
upgraded = 0
for em, sd in smap.items():
    ld = lmap.get(em)
    if ld is None:
        local.append(sd)          # 本地没有的账号整条加进来
        lmap[em] = sd
        added += 1
        if (sd.get("api_key") or "").strip():
            keyed_new += 1
    else:
        skey = (sd.get("api_key") or "").strip()
        lkey = (ld.get("api_key") or "").strip()
        if skey and not lkey:      # 本地缺 key、服务器有 → 补
            before = dict(ld)
            ld.update(sd)
            # 保留本地已有且服务器没有的非空字段（update 不会删，但保守起见）
            for k, v in before.items():
                if v and not ld.get(k):
                    ld[k] = v
            upgraded += 1

with open(LOCAL, "w", encoding="utf-8") as f:
    json.dump(local, f, ensure_ascii=False, indent=1)

# 同步快照（与项目 ledger 结构一致）
snap_dir = os.path.join(ROOT, "ledger", "runs", time.strftime("%Y-%m-%d"))
os.makedirs(snap_dir, exist_ok=True)
snap = os.path.join(snap_dir, f"results-sync-{time.strftime('%Y%m%d-%H%M%S')}.json")
with open(snap, "w", encoding="utf-8") as f:
    json.dump(local, f, ensure_ascii=False, indent=1)

# 纯 key 清单（含账号，便于对账）
ks = [d for d in local if (d.get("api_key") or "").strip()]
ks.sort(key=lambda d: d.get("email", ""))
exp_dir = os.path.join(ROOT, ".workbuddy-ai", "exports")
os.makedirs(exp_dir, exist_ok=True)
exp = os.path.join(exp_dir, f"keys_sync_{time.strftime('%Y%m%d-%H%M%S')}.txt")
with open(exp, "w", encoding="utf-8") as f:
    f.write(f"# OpenXLab API keys 同步快照 {time.strftime('%F %T')}\n")
    f.write(f"# 共 {len(ks)} 个 key（email | key）\n")
    for d in ks:
        f.write(f"{d.get('email','')} | {d['api_key']}\n")

print(f"本地原有 {len(lmap)-added} 条 → 现在共 {len(local)} 条")
print(f"  新增账号 {added}（带key {keyed_new}）")
print(f"  补上 key 的账号 {upgraded}")
print(f"  本地 key 总数 {len(ks)}")
print(f"快照: {snap}")
print(f"清单: {exp}")
