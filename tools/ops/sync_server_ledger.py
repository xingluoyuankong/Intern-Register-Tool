#!/usr/bin/env python3
"""服务器台账 → 本地合并同步（**防丢失**）。

背景
----
服务器容器 overlay 层会被平台重置（2026-10-12 就发生过：`/root` 整个被清，
项目连同台账一起没了）。所以**本地台账必须作为权威超集**，定期把服务器上
注册循环产出的 api_key 拉回来合并，绝不允许服务器数据丢失。

本地台账是"唯一真源"：服务器丢了能从这里恢复；本地永远不会因为同步而缩水。

流程（严格按序，任一步失败都不破坏本地）
----------------------------------------
  1. 探测服务器最新台账快照，scp 拉回临时文件；
  2. **归档原始副本**到 `ledger/backups/`（server + local 各一份，带时间戳）
     —— 就算合并逻辑有 bug，原始数据也永远留底；
  3. 把服务器记录**无损合并**进本地（union，字段只增不减，
     **任何一侧已有的 api_key 都不会被空值覆盖**）；
  4. 落盘新快照（`ledger.save_snapshot`，自带"不许缩水"护栏）；
  5. 把合并后的全量台账**推回服务器**（让服务器看板也有完整数据）。

用法
----
    python tools/ops/sync_server_ledger.py            # 正常同步
    python tools/ops/sync_server_ledger.py --dry-run  # 只报告不落盘
    python tools/ops/sync_server_ledger.py --no-push  # 不推回服务器

环境变量
--------
    IR_SYNC_HOST          默认 qwenpaw-tom（~/.ssh/config 里的别名）
    IR_SERVER_LEDGER_DIR  默认 /root/intern-register/ledger/runs
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import ledger  # noqa: E402

HOST = os.getenv("IR_SYNC_HOST", "qwenpaw-tom")
SERVER_LEDGER_DIR = os.getenv("IR_SERVER_LEDGER_DIR",
                              "/root/intern-register/ledger/runs")
BACKUP_DIR = ROOT / "ledger" / "backups"
LOG = ROOT / "logs" / "sync_server_ledger.log"

_EMPTY = (None, "", [], {})
_RANK = {"success": 2, "skipped": 1}


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _ssh(remote_cmd: str, *, timeout: int = 60) -> str:
    exe = shutil.which("ssh") or "ssh"
    r = _run([exe, "-o", "StrictHostKeyChecking=no",
              "-o", "ConnectTimeout=20", HOST, remote_cmd], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ssh 失败({r.returncode}): {r.stderr.strip()[:300]}")
    return r.stdout


def _scp_pull(remote_path: str, local_path: Path) -> None:
    exe = shutil.which("scp") or "scp"
    r = _run([exe, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20",
              f"{HOST}:{remote_path}", str(local_path)], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"scp 拉取失败({r.returncode}): {r.stderr.strip()[:300]}")


def _scp_push(local_path: Path, remote_path: str) -> None:
    exe = shutil.which("scp") or "scp"
    r = _run([exe, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20",
              str(local_path), f"{HOST}:{remote_path}"], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"scp 推送失败({r.returncode}): {r.stderr.strip()[:300]}")


def _has(d: dict, k: str) -> bool:
    return bool((d.get(k) or "").strip())


def _rank(rec: dict) -> int:
    return _RANK.get(rec.get("status"), 0)


def merge_lossless(local: list[dict], server: list[dict]) -> list[dict]:
    """把 server 记录无损并入 local。

    规则（**永不丢数据**）：
      - 按 email 去重；无 email 的记录原样保留；
      - 字段并集：只增不减，空值不覆盖非空；
      - **api_key 特护**：任一侧已有的非空 key 都不会被覆盖掉
        （本地优先；本地没有才用服务器的）；
      - status 取 rank 更高的（不让失败盖掉成功）。

    返回合并后的全量列表（local 顺序在前）。
    """
    out: list[dict] = []
    idx: dict[str, int] = {}
    seen_noemail: set[str] = set()

    def absorb(rec: dict) -> None:
        email = (rec.get("email") or "").strip()
        if not email:
            # 无 email 的记录按内容去重，否则每次同步都会重复追加一份
            sig = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            if sig in seen_noemail:
                return
            seen_noemail.add(sig)
            out.append(dict(rec))
            return
        if email not in idx:
            idx[email] = len(out)
            out.append(dict(rec))
            return
        cur = out[idx[email]]
        cur_rank, new_rank = _rank(cur), _rank(rec)
        for k, v in rec.items():
            if v in _EMPTY:
                continue                       # 空值不参与覆盖
            if k not in cur or cur[k] in _EMPTY:
                cur[k] = v                     # 补齐本地缺失的字段
            else:
                if k == "api_key":
                    continue                   # 已有 key 绝不被覆盖
                if new_rank > cur_rank:
                    cur[k] = v
        if new_rank > cur_rank and rec.get("status"):
            cur["status"] = rec["status"]

    for r in local:
        absorb(r)
    for r in server:
        absorb(r)
    return out


def _counts(recs: list[dict]) -> tuple[int, int, int]:
    reg = [d for d in recs if _has(d, "sso_uid")]
    return len(recs), len(reg), sum(1 for d in recs if _has(d, "api_key"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告不落盘")
    ap.add_argument("--no-push", action="store_true", help="不推回服务器")
    args = ap.parse_args()

    _log("=" * 60)
    _log(f"同步开始 host={HOST} dry_run={args.dry_run} no_push={args.no_push}")

    # ── 1. 探测 + 拉取服务器最新快照 ────────────────────────────
    try:
        remote_latest = _ssh(
            f"ls -1 {SERVER_LEDGER_DIR}/*/*.json 2>/dev/null | tail -1").strip()
    except Exception as e:
        _log(f"✗ 无法访问服务器: {e}")
        return 2
    if not remote_latest:
        _log("✗ 服务器上没有台账快照，跳过（不触碰本地）")
        return 3
    _log(f"服务器最新快照: {remote_latest}")

    tmpdir = Path(tempfile.mkdtemp(prefix="ir_sync_"))
    tmp_server = tmpdir / "server.json"
    try:
        _scp_pull(remote_latest, tmp_server)
    except Exception as e:
        _log(f"✗ 拉取失败: {e}")
        return 2
    server_recs = ledger.load_existing(tmp_server)
    if not server_recs:
        _log("✗ 服务器快照为空/损坏，跳过（不触碰本地）")
        return 3

    # ── 2. 本地台账 ────────────────────────────────────────────
    local_path = ledger.ledger_path()
    local_recs = ledger.load_existing(local_path)
    lt, lr, lk = _counts(local_recs)
    st, sr, sk = _counts(server_recs)
    _log(f"本地: 总{lt} 已注册{lr} 有key{lk}   (源 {local_path.name})")
    _log(f"服务器: 总{st} 已注册{sr} 有key{sk}")

    # ── 3. 归档原始副本（永远留底）─────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    b_local = BACKUP_DIR / f"local-{ts}.json"
    b_server = BACKUP_DIR / f"server-{ts}.json"
    if not args.dry_run:
        b_local.write_text(json.dumps(local_recs, ensure_ascii=False),
                           encoding="utf-8")
        b_server.write_text(json.dumps(server_recs, ensure_ascii=False),
                            encoding="utf-8")
        _log(f"原始副本已归档: {b_local.name} / {b_server.name}")

    # ── 4. 无损合并 ────────────────────────────────────────────
    merged = merge_lossless(local_recs, server_recs)
    mt, mr, mk = _counts(merged)
    _log(f"合并后: 总{mt} 已注册{mr} 有key{mk}  "
         f"(新增key {mk - lk}, 新增账号 {mt - lt})")

    # 安全护栏：合并后绝不能比本地少
    if mk < lk or mt < lt:
        _log(f"✗ 护栏触发：合并后(总{mt}/key{mk}) < 本地(总{lt}/key{lk})，"
             f"拒绝落盘")
        return 4

    if args.dry_run:
        _log("dry-run，不落盘。结束。")
        return 0

    # ── 5. 落盘本地快照 ────────────────────────────────────────
    try:
        snap, _ = ledger.save_snapshot(merged, existing=local_recs)
    except ValueError as e:
        _log(f"✗ 落盘护栏拦截: {e}")
        return 4
    _log(f"✓ 本地新快照: {snap}")

    # ── 6. 推回服务器（让服务器看板也有完整数据）───────────────
    if not args.no_push:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        remote_dir = f"{SERVER_LEDGER_DIR}/{day}"
        remote_file = f"{remote_dir}/results-{ts}.json"
        try:
            _ssh(f"mkdir -p {remote_dir}")
            _scp_push(snap, remote_file)
            _log(f"✓ 已推回服务器: {remote_file}")
        except Exception as e:
            _log(f"⚠ 推回服务器失败（不影响本地）: {e}")

    _log(f"同步完成 ✓  本地最终: 总{mt} 已注册{mr} 有key{mk}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # 兜底：异常必落日志
        _log(f"✗✗✗ 未捕获异常: {type(e).__name__}: {e}")
        raise
