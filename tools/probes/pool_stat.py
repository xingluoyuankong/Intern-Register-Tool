from src import ledger
h = lambda x, k: bool((x.get(k) or "").strip())
recs = ledger.load_existing(ledger.ledger_path())
reg = [x for x in recs if h(x, "sso_uid")]
keyed = [x for x in reg if h(x, "api_key")]
print("总条目:", len(recs))
print("已注册(sso_uid):", len(reg))
print("有 key:", len(keyed))
print("已注册未领 key:", len(reg) - len(keyed))
nokey = [x for x in reg if not h(x, "api_key")]
from collections import Counter
print("未领key的 error 分布:", Counter((x.get("error") or "")[:50] for x in nokey).most_common(3))
print("未领key的 status 分布:", Counter(x.get("status") for x in nokey).most_common(3))
# 未注册成功的
notreg = [x for x in recs if not h(x, "sso_uid")]
print("未注册成功:", len(notreg))
print("未注册的 error 分布:", Counter((x.get("error") or "")[:50] for x in notreg).most_common(3))
