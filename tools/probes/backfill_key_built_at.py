"""给存量 key 记录补 `key_built_at`（一次性回填）。

依据：台账 runs/*.json 是**逐账号落盘**的（2026-10-13 起收割就是每账号
flush 一次），快照文件 mtime ≈ 该记录 key 建成的时间（UTC 磁盘时间）。
回填规则：该 email 有 api_key 且缺 key_built_at → 取**包含它的最新快照**
的 mtime（UTC 格式化为 created_at 同款 `%Y-%m-%d %H:%M:%S`）。
"""
import json
import glob
import os
from datetime import datetime, timezone

ROOT = "/app/working/intern-register"
RUNS = os.path.join(ROOT, "ledger", "runs", "*", "*.json")

# email -> (key_built_at 候选, 快照路径)
best = {}
for f in sorted(glob.glob(RUNS)):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    mt = datetime.fromtimestamp(os.path.getmtime(f), tz=timezone.utc)
    stamp = mt.strftime("%Y-%m-%d %H:%M:%S")
    for r in (d if isinstance(d, list) else [d]):
        if not isinstance(r, dict):
            continue
        em = r.get("email")
        if not em or not (r.get("api_key") or "").strip():
            continue
        if r.get("key_built_at"):
            continue
        if em not in best or f > best[em][1]:
            best[em] = (stamp, f)

print(f"需回填 {len(best)} 条")

# 重新扫一遍写回：按文件粒度更新（同一文件可能含多条）
changed_files = set()
for f in sorted(glob.glob(RUNS)):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    if not isinstance(d, list):
        continue
    dirty = False
    for r in d:
        em = r.get("email")
        if em in best and best[em][1] == f and not r.get("key_built_at"):
            r["key_built_at"] = best[em][0]
            dirty = True
    if dirty:
        json.dump(d, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        changed_files.add(f)

print(f"更新了 {len(changed_files)} 个快照文件")

# 重算 latest.json 并验证
import subprocess
subprocess.run([".venv/bin/python", "-c",
                "from src import ledger;\n"
                "import json;\n"
                "recs=ledger.load_existing(ledger.ledger_path());\n"
                "json.dump(recs, open('ledger/latest.json','w',encoding='utf-8'), ensure_ascii=False, indent=1)"],
               cwd=ROOT, check=True)

recs = json.load(open(os.path.join(ROOT, "ledger", "latest.json"), encoding="utf-8"))
ks = [x for x in recs if (x.get("api_key") or "").strip()]
from collections import Counter
c = Counter((x.get("key_built_at") or "空")[:10] for x in ks)
print("回填后 key_built_at 分布(top8):", c.most_common(8))
