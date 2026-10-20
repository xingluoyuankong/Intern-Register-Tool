"""小时级 key 同步器（2026-10-17）：台账 → GitHub 私库。

用法（由 supervisor 程序 key-sync 每小时调一次，也可手动跑）：
    .venv/bin/python tools/ops/key_sync.py

做什么：
  1. 从台账**最新全量快照**（ledger/runs/<日期>/results-*.json，按文件名序
     取最新——那是台账的读源，见 run.py 的 --out 说明）导出全部有效 key；
  2. 写 key-sync/keys.json（全量）+ README.md（统计 + 最近 5 条）；
  3. keys.json **有变化才** git add/commit/push（无变化不产生空提交）；
  4. 全过程追加日志到 logs/key_sync.log。

凭据：git credential store 指向
`.workbuddy-ai/git-creds`（NAS 持久卷，容器重建不丢；⚠ 0600，勿入库）。
remote URL 本身**不含** token——泄露面只在那一个凭据文件里。
"""

import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))          # intern-register/
SYNC = os.path.join(ROOT, "key-sync")
LOG = os.path.join(ROOT, "logs", "key_sync.log")


def log(msg: str) -> None:
    line = f"{time.strftime('%F %T')}  {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def extract() -> list[dict]:
    """从最新全量快照导出有效 key 记录。"""
    snaps = sorted(glob.glob(os.path.join(
        ROOT, "ledger", "runs", "*", "results-*.json")))
    if not snaps:
        return []
    src = snaps[-1]
    try:
        data = json.load(open(src, encoding="utf-8"))
    except (ValueError, OSError) as ex:
        log(f"⚠ 快照读不了（{src}）：{ex}")
        return []
    if isinstance(data, dict):
        data = data.get("records") or data.get("data") or []
    rows = []
    for r in data:
        k = r.get("api_key") or ""
        if not str(k).startswith("sk-"):
            continue
        rows.append({
            "key": str(k),
            "email": r.get("email", ""),
            "username": r.get("username", ""),
            "sso_uid": r.get("sso_uid", ""),
            "created_at": r.get("created_at", ""),
            "key_built_at": r.get("key_built_at") or r.get("key_created") or "",
            "status": r.get("status", ""),
        })
    # 去重（按 key）：同账号换出口重试成功时可能写两条
    seen: dict = {}
    for r in rows:
        seen[r["key"]] = r
    return [seen[k] for k in sorted(seen)]


def write_files(rows: list[dict], src: str) -> None:
    os.makedirs(SYNC, exist_ok=True)
    keys_path = os.path.join(SYNC, "keys.json")
    with open(keys_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    # 🔴 纯 key 文件（2026-10-17 用户要求）：一行一个，给直接灌脚本/护栏的工具吃
    with open(os.path.join(SYNC, "keys.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(r["key"] for r in rows) + "\n")
    recent = rows[-5:]
    readme = [
        "# OpenXLab API Keys",
        "",
        f"- **总数**：{len(rows)} 个有效 key",
        f"- **读源**：`{os.path.relpath(src, ROOT)}`",
        f"- **最近同步**：{time.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        "",
        "文件：`keys.json`（全量含邮箱/用户名/时间）、`keys.txt`（纯 key，一行一个）。",
        "由 `tools/ops/key_sync.py` 每小时自动同步（有新增才提交）。",
        "",
        "## 最近 5 条",
        "",
        "| 时间 | 邮箱 | key |",
        "|---|---|---|",
    ] + [
        f"| {r['created_at']} | {r['email']} | `{r['key'][:20]}…` |"
        for r in recent
    ] + [
        "",
        "## keys.txt 预览（最近 3 个）",
        "",
        "```",
    ] + [r["key"] for r in recent[-3:]] + ["```"]
    with open(os.path.join(SYNC, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(readme) + "\n")


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=SYNC, capture_output=True, text=True,
                          timeout=120)


def main() -> int:
    rows = extract()
    snaps = sorted(glob.glob(os.path.join(
        ROOT, "ledger", "runs", "*", "results-*.json")))
    src = snaps[-1] if snaps else "?"
    if not rows:
        log(f"⚠ 没导出任何 key（读源 {src}）—— 台账为空还是坏了？")
        return 1

    keys_path = os.path.join(SYNC, "keys.json")
    txt_path = os.path.join(SYNC, "keys.txt")
    old = old_txt = ""
    try:
        old = open(keys_path, encoding="utf-8").read()
        old_txt = open(txt_path, encoding="utf-8").read()
    except OSError:
        pass

    write_files(rows, src)
    new = open(keys_path, encoding="utf-8").read()
    new_txt = open(txt_path, encoding="utf-8").read()
    if new == old and new_txt == old_txt:
        log(f"无变化（{len(rows)} 个 key）—— 不提交")
        return 0

    # 只在密钥集合变化时记录“新增了几个”，说清楚这次提交的实质
    try:
        old_set = {r["key"] for r in json.loads(old or "[]")}
    except ValueError:
        old_set = set()
    added = [r for r in rows if r["key"] not in old_set]
    r = sh("git", "add", "keys.json", "keys.txt", "README.md")
    r = sh("git", "commit", "-q", "-m",
           f"sync: {len(rows)} keys (+{len(added)} new) "
           f"{time.strftime('%F %H:%M')}")
    if r.returncode != 0:
        log(f"⚠ commit 失败：{r.stderr.strip()[:120]}")
        return 1
    r = sh("git", "push", "origin", "main")
    if r.returncode != 0:
        log(f"⚠ push 失败：{r.stderr.strip()[-200:]}")
        return 1
    log(f"✅ 已推送 {len(rows)} 个 key（新增 {len(added)}）"
        f"—— {', '.join(x['email'] for x in added[:3])}"
        f"{'…' if len(added) > 3 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
