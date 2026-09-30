"""把 OOM 丢失的 key 记录补录进服务器台账（batch1 的 2 条 + batch3 的 7 条）。

这些记录有 email/key、没有密码（进程死在写台账前，密码随之丢失）——
password 置 `(lost-oom)` 明示不可再登录，key 本身仍可用。
用项目 ledger.save 落一个新 runs 快照 + 刷新 latest.json，格式与正常批次一致。
"""
import json
import sys
import time

sys.path.insert(0, ".")
from src import ledger  # noqa: E402


def main() -> None:
    recs = []
    b1 = [
        ("porschestefanow68+djj57x2lpu@outlook.com",
         "sk-9270abc610094b95c9c0f225d08b0b2eaf066f78ad3b43f716915fc771de7181"),
        ("aidanmacqueen42+49evavj5fw@outlook.com",
         "sk-e83472b943aa51b6c39bafa37082b5061d5a7b48e9c21acf0e066fbf079e35cf"),
    ]
    for email, key in b1:
        recs.append({"email": email, "api_key": key, "status": "ok",
                     "password": "(lost-oom)", "note": "batch1 OOM 补录"})
    for r in json.load(open("keys_batch3.json", encoding="utf-8")):
        recs.append({"email": r["email"], "api_key": r["api_key"],
                     "status": "ok", "password": "(lost-oom)",
                     "note": f"batch3 slot{r['slot']} OOM 补录"})

    existing = ledger.load_existing("ledger/latest.json")
    merged = existing + recs
    out = f"ledger/runs/{time.strftime('%Y-%m-%d')}/results-backfill-oom.json"
    ledger.save(out, merged)
    print(f"backfilled {len(recs)} -> {out} (total {len(merged)})")


if __name__ == "__main__":
    main()
