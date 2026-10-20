import json, glob
from collections import Counter

fs = sorted(glob.glob("/app/working/intern-register/ledger/runs/*/*.json"))
d = []
for f in fs:
    try:
        d.extend(json.load(open(f, encoding="utf-8")))
    except Exception:
        pass
h = lambda x, k: bool((x.get(k) or "").strip())
print("台账文件数:", len(fs))
print("总条目:", len(d))
reg = [x for x in d if h(x, "sso_uid")]
print("已注册(sso_uid):", len(reg))
keyed = [x for x in reg if h(x, "api_key")]
print("有 api_key:", len(keyed))
nokey = [x for x in reg if not h(x, "api_key")]
print("已注册但没 key:", len(nokey))
print("没key的状态分布:", Counter(x.get("status") for x in nokey).most_common(5))
print("没key的error分布:", Counter((x.get("error") or "")[:60] for x in nokey).most_common(5))
if nokey:
    x = nokey[-1]
    print("样例:", {k: x.get(k) for k in ("email", "username", "password", "sso_uid", "status", "error")})
    # 导出这些账号供登录建 key
    out = [{k: x2.get(k) for k in ("email", "username", "password", "sso_uid", "status")}
           for x2 in nokey]
    with open("/app/working/intern-register/nokey_accounts.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("已导出 -> /app/working/intern-register/nokey_accounts.json")
