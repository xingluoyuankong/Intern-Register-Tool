"""对**已有账号**跑通下游全链路：登录 → 查额度 → 建/复用 Key → 真发推理。

为什么需要这个工具
------------------
`run.py` 跑的是**完整**链路，第一步就是注册 —— 而注册当前被封（`B0000`，
IP 维度累计配额）。这意味着**注册之后的每一段都测不了**，哪怕它们和封禁
毫无关系（登录走 SSO、建 Key 走 discovery、推理走 discovery-api，三个
不同主机）。

这个工具从台账（`ledger/runs/<日期>/results-<时间戳>.json`，读源 = 最新那份
全量快照）里取**已经注册成功**的账号，只跑下游：

    Stage 3  登录（Playwright，取 JWT + 浏览器 Cookie）
    Stage 4  查额度（纯只读：getUserInfo / free-grant-status / balance / list_keys）
    Stage 5  建/复用 API Key（`ensure_key` 幂等）
    Stage 6  真发一次 chat/completions —— 唯一能证明"这个账号真能用"的判据

**全程零注册请求**，因此不会加深封禁，可以随便跑。

为什么要复用 `src/ledger.py` 而不是自己写 `--out`
-------------------------------------------------
台账同时是"运行报告"和"账号台账"。本项目已经因为"直接覆盖"
丢过一次台账（53 条 → 1 条）。所以这里的写盘走 `ledger.save()`，
目标在台账目录里时走 `ledger.save_snapshot()`（留一份日期/时间戳快照，
它随即成为新读源）：按 email 合并、失败不盖成功、条数不得变少的护栏。**任何会写台账的工具
都必须复用同一个合并实现**，不能各写各的。

🔴 三个必须小心的点
-------------------
1. **`ensure_key` 命中已有 key 时返回 `key=""`**（列表接口不返回明文）。
   所以复用分支**绝不能**用它去覆盖 `rec.api_key` —— 那会把一把好 key
   洗成空字符串。本工具用 `or` 兜底：新明文优先，否则保留原值。

2. **`create_key` 是非幂等的**（每次新 `Idempotency-Key`，每次建一把新的）。
   默认**不建**，只做幂等复用；要验证"建 Key"这条路径得显式给 `--create N`。
   否则每跑一次工具就在每个账号上多堆一把 key。

3. **`GET/POST /tokenplan/v1/keys` 只带 Authorization 会 `-10002`**，
   必须同时带浏览器会话 Cookie。所以 Stage 5 不能省掉浏览器登录。

用法：
    python tools/run_downstream.py                      # 4 个账号，只读+复用
    python tools/run_downstream.py --limit 12 --workers 4
    python tools/run_downstream.py --create 2           # 其中 2 个真建新 key
    python tools/run_downstream.py --no-write           # 只看结果，不落盘
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from _bootstrap import ROOT  # noqa: F401  （副作用：把仓库根加进 sys.path）

from src import cli as _cli  # noqa: E402  （必须在 _bootstrap 之后）
from src import ledger  # noqa: E402  （必须在 _bootstrap 之后：它才把仓库根加进 sys.path）

# 台账**读源** = `runs/` 里最新的全量快照。注意它是**每次落盘都换名字**的，
# 不是常量路径 —— `ledger.ledger_path()` 现扫目录算出来。
# 写盘默认也回到这里，但**经过 `save_snapshot()`**：每次落盘留一份新的
# `ledger/runs/<日期>/results-<时间戳>.json`，它随即成为新读源。
#
# ⚠ 模块级取值会**冻住 import 时刻**那份快照。这里无害：写盘走
#   `is_ledger_path()` 判"在不在台账目录"，与具体是哪一个文件无关。
#   真要"当下的读源"就现调 `ledger.ledger_path()`，别读这两个常量。
DEFAULT_IN = ledger.ledger_path()
DEFAULT_OUT = ledger.ledger_path()


# ────────────────────────────────────────────────────────────────
# 记录构造
# ────────────────────────────────────────────────────────────────
def _to_record(d: dict):
    """dict → AccountRecord。只喂 dataclass 认识的字段。

    🔴 不能用 `AccountRecord(**d)` 直接展开：台账里混着历史字段
    （`source` / `verify` 是早期版本写的），多一个未知 key 就 TypeError，
    整个工具跑不起来。这里显式取交集，未知字段原样留在 dict 里不丢。
    """
    from dataclasses import fields

    from src.pipeline import AccountRecord

    known = {f.name for f in fields(AccountRecord)}
    return AccountRecord(**{k: v for k, v in d.items() if k in known})


def _eligible(d: dict) -> bool:
    """这个账号能不能跑下游。

    判据 = **有 email + 有 password**。不需要 jwt —— jwt 只有 14 天有效期，
    而且下游第一步就是重新登录拿一个新的，本来就该以"能不能登录"为准。
    """
    return bool((d.get("email") or "").strip() and (d.get("password") or "").strip())


def _login_id(d: dict) -> str:
    """登录页账号框该填什么 —— 委托给**共享判据** `pipeline.login_identifier`。

    为什么不让这里自己写一份：`run.py` 主链路（`pipeline.stage_login_key`）
    与收割工具必须用**同一条**判据，否则两边行为分叉（一边能登录、一边不能），
    排查时会被这种分叉骗。详细理由（40 字符前端校验 + 用户名登录）见
    `src/pipeline.py:login_identifier`。
    """
    from src.pipeline import login_identifier

    return login_identifier(d.get("email", ""), d.get("username", ""))


# ────────────────────────────────────────────────────────────────
# 单账号：Stage 3~6
# ────────────────────────────────────────────────────────────────
def run_one(d: dict, *, headless: bool, create: bool, key_name: str,
            rekey: bool = False, log=print) -> dict:
    """跑一个账号的下游链路，返回**要合并回台账的字段**（不整条覆盖）。

    返回 dict 而不是 AccountRecord：台账里那 38 条从 CSV 恢复的记录字段
    比 dataclass 少，整条覆盖会把它们已有的 `source` 等字段洗掉。

    🔴 本函数与 `src/pipeline.py:stage_login_key` 是**同一段下游链路的两个版本**，
       差异是**有意的**，别"顺手统一" —— 逐字段 ceiling 表与"为什么不能合并"
       见 `docs/audit-2026-09-20.md` §2.2「B7a」。最容易踩的两条：
         · 这里**调** `list_keys()` 并落 `balance_raw` 全量（pipeline 两件都不做）。
         · 这里的校验含 `chat()` 真发一次推理（pipeline 只 `list_models`）。
       两边的调用序列由 `tests/test_downstream_divergence.py` 钉住。
    """
    from src.discovery import DiscoveryClient

    rec = _to_record(d)
    out: dict = {"email": rec.email}
    t_all = time.time()
    timings: dict = {}

    # ── Stage 3：登录 ─────────────────────────────────────────
    t = time.time()
    login_id = _login_id(d)
    try:
        from src.browser import BrowserSession

        with BrowserSession(headless=headless) as sess:
            # 🔴 只试 1 次：与 `pipeline.stage_login_key` 同理由 —— 失败多是
            #    账号/出口维度的问题，原地重试 3 次 = 白烧 ~8 分钟
            #    （3×150s 超时 + 冷却）。换号才有意义。收割 80+ 账号时这是关键。
            res = sess.login(login_id, rec.password, verbose=False, attempts=1)
            timings["browser_launch"] = sess.launch_ms
            if not res.ok:
                raise RuntimeError(res.reason or "login failed")
            rec.jwt = res.jwt
            timings["login"] = round((time.time() - t) * 1000)
            timings["login_detail"] = res.timings
            out["jwt"] = res.jwt
            out["login_ms"] = timings["login"]
            cs = res.captcha_stage or {}
            log(f"✓ 登录 {timings['login']}ms  jwt={len(res.jwt)}B  "
                f"captcha={cs.get('path', '?')}")
    except Exception as ex:                                       # noqa: BLE001
        out["downstream"] = f"login_failed: {str(ex)[:160]}"
        out["login_ms"] = round((time.time() - t) * 1000)
        log(f"✗ 登录失败：{str(ex)[:140]}")
        return out

    # ── Stage 4：只读（user_info / grant / balance / list_keys）──
    t = time.time()
    try:
        dc = DiscoveryClient(jwt=rec.jwt, cookies=res.cookies)

        info = dc.get_user_info()
        out["sso_username"] = info.get("sso_username", "")
        out["sso_email"] = info.get("sso_email", "")

        grant = dc.free_grant_status()
        out["grant_has_received"] = grant.get("has_received")
        claimed = False
        if not grant.get("has_received"):
            # 幂等：没领过才领。已经领过就别再打这个接口。
            dc.claim_free_grant()
            claimed = True
            out["grant_has_received"] = True
        out["grant_claimed_now"] = claimed

        bal = dc.balance()
        out["credits"] = str(bal.get("available_credits", ""))
        # 🔴 原始 balance 全量留档。上一轮发现 12 个账号里有
        #    `6.547000` / `9.936000` / `9.999000` 这种非 10 的取值，
        #    只存 `available_credits` 一个数查不出原因（是被消耗了？
        #    还是发放口径不同？），必须连 `usage_windows` 一起留下。
        out["balance_raw"] = bal

        keys = dc.list_keys()
        out["key_count"] = len(keys)
        out["key_names"] = [k.get("name", "") for k in keys]

        timings["readonly"] = round((time.time() - t) * 1000)
        log(f"✓ 只读 {timings['readonly']}ms  credits={out['credits']}  "
            f"keys={out['key_count']}{[k.get('name') for k in keys]}")
    except Exception as ex:                                       # noqa: BLE001
        out["downstream"] = f"readonly_failed: {str(ex)[:160]}"
        timings["readonly"] = round((time.time() - t) * 1000)
        log(f"✗ 只读失败：{str(ex)[:140]}")
        return out

    # ── Stage 5：建/复用 Key ──────────────────────────────────
    t = time.time()
    plaintext = ""
    try:
        if create:
            # 非幂等：显式要才建。名字带时间戳，避免和已有的 default 撞名
            # 从而"以为建了其实只是复用了"。
            name = f"{key_name}-{time.strftime('%m%d-%H%M%S')}"
            ak = dc.create_key(name)
            plaintext = ak.key
            out["key_created"] = name
            # 🔴 **新建的明文必须落盘** —— 这是 `--create` 的唯一产出。
            #
            # 2026-09-19 修：原先这里只把明文存进局部变量 `plaintext` 用于
            # Stage 6 验证，**从不写进 `out`** ⇒ 下游跑完、验证也过了
            # （`verify=ok(10 models)`），但台账里 `api_key` 是空的 ——
            # 账号可用、key 却拿不回来，等于白建。
            # 而且 `create_key` 是**非幂等**的（见上面第 2 点），
            # 想再拿一次只能再建一把，凭空多一把孤儿 key。
            #
            # ⚠ 与**复用分支相反**：复用命中的 key 列表接口不返回明文
            #   （`ak.key == ""`），那种情况绝不能赋值，否则会把已有的
            #   好 key 洗成空串。所以这里必须判 `if ak.key`。
            if ak.key:
                out["api_key"] = ak.key
            log(f"✓ 新建 key「{name}」= {ak.key[:12]}…")
        else:
            ak = dc.ensure_key(key_name)
            # 🔴 命中已有 key 时 `ak.key` 是空串（列表不给明文）。
            #    绝不能用它覆盖 rec.api_key —— 见模块 docstring 第 1 点。
            plaintext = ak.key
            if not plaintext and rekey:
                # 🔴 2026-10-05：已有同名 key 但明文拿不回来（列表接口不返回）。
                #    为了拿到**可用明文**只能另建一把 —— `create_key` 非幂等，
                #    名字带时间戳避免和已有的撞名。收割「已注册未领 key」专用：
                #    那些账号往往在服务端已有孤儿 key（早期"复用分支丢明文"的
                #    遗留），但明文已不可考，必须 rekey 才能拿到可用 key。
                name = f"{key_name}-h{time.strftime('%m%d-%H%M%S')}"
                ak = dc.create_key(name)
                plaintext = ak.key
                out["key_created"] = name
                log(f"✓ 已有 key「{key_name}」无明文 → 另建「{name}」= "
                    f"{ak.key[:12]}…")
            else:
                # 🔴 2026-10-05 修：`ensure_key` 对**没有该名 key 的账号会新建**
                #    并返回明文。旧实现只在 create 分支写 `out["api_key"]`，
                #    于是"复用"分支新建出来的明文被丢掉 —— 收割无 key 账号时
                #    表现为"key 建了但台账里是空的、拿不回来"。这里补上
                #    （仅当有明文才写，避免把已有好 key 洗成空串）。
                out["key_created"] = "" if ak.key else None
                log(f"✓ 复用/新建 key「{key_name}」"
                    f"{'（无明文，列表接口不返回）' if not ak.key else ''}")
            if ak.key:
                out["api_key"] = ak.key
        out["key_id"] = ak.id
        out["key_masked"] = ak.masked_key
        out["key_status"] = ak.status
        # 🔴 面板"今日 Key"的统计依据：**key 建成时间**（UTC，与 created_at
        #    同格式）。没有它，收割今天建出的 key 会按老 created_at（注册日）
        #    统计 → 面板永远"今日 0"（2026-10-14 实测）。
        if ak.key:
            out["key_built_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        timings["key"] = round((time.time() - t) * 1000)
    except Exception as ex:                                       # noqa: BLE001
        out["downstream"] = f"key_failed: {str(ex)[:160]}"
        timings["key"] = round((time.time() - t) * 1000)
        log(f"✗ 建 Key 失败：{str(ex)[:140]}")
        return out

    # ── Stage 6：真发一次推理 ─────────────────────────────────
    # 明文 key 的优先来源：本次新建 > 台账里已有的。
    # 台账里那把是**同一个账号**的 key，用它验证等价于验证这个账号。
    verify_key = plaintext or (d.get("api_key") or "")
    out["verify_key_source"] = ("new" if plaintext else
                                "ledger" if d.get("api_key") else "none")
    if verify_key:
        t = time.time()
        try:
            from src import apikey as _ak

            # 🔴 **新建的 key 有传播延迟**：`POST /tokenplan/v1/keys` 已经返回
            #    sk-...，但立刻拿去打 `/v1/models` 会 **401 Unauthorized**。
            #    2026-09-18 实测：`--create 1` 那个账号就这样报了 401，
            #    而它明明刚建成功 —— 这不是 key 无效，是还没在网关侧生效。
            #    复用路径不需要等（老 key 早就生效了），只对新建的等。
            if plaintext:
                _ak.wait_until_active(verify_key, attempts=4, delay=3.0)

            models = _ak.list_models(verify_key)
            out["verify_models"] = len(models)
            res2 = _ak.chat(verify_key, "只回复两个字：成功", max_tokens=128)
            timings["verify"] = round((time.time() - t) * 1000)
            if res2.ok and not res2.truncated:
                out["verify"] = f"ok({len(models)} models)"
                out["verify_reply"] = res2.text[:40]
                out["verify_usage"] = res2.usage
                log(f"✓ 推理 {timings['verify']}ms  {len(models)} models  "
                    f"reply={res2.text[:20]!r}  usage={res2.usage.get('total_tokens')}")
            elif res2.ok and res2.truncated:
                # 网关通了、模型在推理，只是 reasoning 把 max_tokens 吃光了。
                # 这不是失败 —— 见 src/apikey.py 的 ChatResult 注释。
                out["verify"] = f"ok({len(models)} models, 正文被截断)"
                out["verify_reply"] = ""
                log(f"✓ 推理 {timings['verify']}ms  {len(models)} models  "
                    f"正文被 reasoning 截断（非失败）")
            else:
                out["verify"] = f"failed: {res2.error[:120]}"
                log(f"✗ 推理失败：{res2.error[:140]}")
        except Exception as ex:                                   # noqa: BLE001
            msg = f"{type(ex).__name__}: {ex}"[:160]
            # 401 + 本次是新建的 key = 传播还没到位，**不是** key 坏了。
            # 旧实现直接写 `failed`，会把一个刚建成功的账号误报成失败。
            if plaintext and "401" in msg:
                out["verify"] = "not active yet(401, 传播延迟)"
                log("⚠ 新建 key 网关侧尚未生效（401），属传播延迟，非失败")
            else:
                out["verify"] = f"failed: {msg[:120]}"
                log(f"✗ 推理异常：{msg[:140]}")
            timings["verify"] = round((time.time() - t) * 1000)
    else:
        out["verify"] = "skipped: no plaintext key"

    out["downstream"] = "ok"
    out["downstream_ms"] = round((time.time() - t_all) * 1000)
    out["timings_downstream"] = timings
    out["downstream_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # 显式清空 `error`：合并是**并集**（见 src/ledger.merge_records），
    # 老记录里若留着上次的失败原因，不清就会挂在一个已经跑通的账号上。
    out["error"] = ""
    return out


# ────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="对已有账号跑下游全链路（零注册请求）")
    ap.add_argument("--from", dest="src", default=str(DEFAULT_IN),
                    help="账号台账来源")
    ap.add_argument("--limit", type=int, default=4, help="跑几个账号（0=全部）")
    ap.add_argument("--workers", type=int, default=4, help="并发数（受内存约束）")
    ap.add_argument("--create", type=int, default=0,
                    help="其中前 N 个**真建新 key**（验证建 Key 路径）。"
                         "0=全部只做幂等复用（默认，不污染 key 列表）")
    ap.add_argument("--key-name", default="default", help="复用/新建的 key 名")
    # `--headless` / `--headful` 的接线与 `run.py` 共用（见 src/cli.py）。
    # ⚠ 别把模块级 `DEFAULT_IN` / `DEFAULT_OUT` 也搬进 src/cli.py ——
    #   它们是**冻住 import 时刻**那份快照的取值（上面 :65-67 写了为什么无害），
    #   搬走会让"什么时候取值"从可见变成不可见。
    _cli.add_headless_args(ap)
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="台账输出。默认 = 台账读源，此时落"
                         "ledger/runs/<日期>/results-<时间戳>.json 快照（随即成为新读源）"
                         "+ 刷 ledger/latest.json（本批结果）；"
                         "给别的路径则只写那个文件")
    ap.add_argument("--no-write", action="store_true", help="不写台账，只打印")
    ap.add_argument("--keyless-only", action="store_true",
                    help="只处理台账里**没有 api_key** 的账号（收割已注册未领 key）")
    ap.add_argument("--include-burned-domains", action="store_true",
                    help="也收割烧穿域（hotmail/outlook/gmail…）的账号。"
                         "默认跳过：这些号登录恒弹滑块（2026-10-18 实测），"
                         "纯烧浏览器时间。")
    ap.add_argument("--rekey", action="store_true",
                    help="复用分支遇到「已有 key 但列表接口不给明文」时，"
                         "**另建一把新 key** 以拿到可用明文（create_key 非幂等）。"
                         "收割孤儿 key 账号时必须开 —— 否则明文永远取不回来。")
    ap.add_argument("--registered-only", action="store_true",
                    help="只处理**已注册成功**（sso_uid 非空）的账号。"
                         "台账里大量记录是「邮箱拿到但 SSO 注册失败」，"
                         "它们没有 sso_uid、登录必然失败 —— 收割时必须剔掉，"
                         "否则 813 条里混着 600+ 条注定失败的，纯空转。"
                         "配合 --keyless-only = 「已注册未领 key」的精确收割集。")
    args = ap.parse_args()

    from src import ledger

    src_path = Path(args.src)
    existing = ledger.load_existing(src_path)
    if not existing:
        print(f"✗ 读不到台账：{src_path}")
        return 1

    pool = [d for d in existing if _eligible(d)]
    if args.registered_only:
        # 🔴 sso_uid 是**注册成功**的判据（注册失败 → register 阶段报错、sso_uid 留空）。
        #    收割「已注册未领 key」必须以此为准，不能只看 api_key 是否为空 ——
        #    空 api_key 的账号里绝大多数是**根本没注册成**的。
        pool = [d for d in pool if (d.get("sso_uid") or "").strip()]
    if args.keyless_only:
        pool = [d for d in pool if not (d.get("api_key") or "").strip()]
        # 🔴 2026-10-18：跳过登录侧已烧穿的邮箱域。这些老号登录恒弹滑块
        #    （T001/F008），白烧浏览器时间 —— 实测当天未激活的新域号登录
        #    3/3 全过（见 pipeline 的 mail-timeout 分支）。要恢复老号收割：
        #    --include-burned-domains。
        if not getattr(args, "include_burned_domains", False):
            _burned = {"hotmail.com", "outlook.com", "gmail.com", "qq.com",
                       "proton.me", "protonmail.com"}
            _kept, _skip = [], 0
            for d in pool:
                dom = str(d.get("email") or "").split("@")[-1].lower()
                if dom in _burned:
                    _skip += 1
                else:
                    _kept.append(d)
            if _skip:
                pool = _kept
                print(f"↷ 跳过 {_skip} 个烧穿域账号（hotmail/outlook/gmail…，"
                      f"登录恒滑块；--include-burned-domains 可恢复）")
    if not pool:
        print(f"✗ {len(existing)} 条记录里没有可用的（需要 email + password）")
        return 1

    # 优先挑有 jwt 的（说明曾经登录成功过，密码大概率仍有效），
    # 再挑有 api_key 的（Stage 6 能直接拿它验，不依赖本次是否建 key）。
    # 🔴 2026-10-18 组内随机（shuffle 在稳定排序前）：否则猎手探针永远打
    #    排序第一名 —— 那个号若被单独风控，窗口开了也探不到。随机后
    #    每次探针换号，窗口检测无盲区；爆发收割顺序也更分散。
    import random as _rnd
    _rnd.shuffle(pool)
    pool.sort(key=lambda d: (not d.get("jwt"), not d.get("api_key")))
    n = len(pool) if args.limit <= 0 else min(args.limit, len(pool))
    targets = pool[:n]

    print(f"台账 {len(existing)} 条 → 可用 {len(pool)} 条 → 本次跑 {n} 条"
          f"（并发 {args.workers}，"
          f"{'建新 key' if args.create else '仅幂等复用'}）")
    print("⚠ 零注册请求：本工具不打注册接口，不会加深 B0000 封禁")
    print("=" * 72)

    lock = threading.Lock()
    results: list[dict] = [None] * n

    def _flush_one(rec: dict) -> None:
        """单账号**立即**写回台账。

        🔴 容器每 ~10 分钟重启：整批跑完才落盘时，批内已建出的 key 明文
        （create_key 非幂等，错过就再也拿不回）会被整批带走 —— 实测同一账号
        被反复 rekey（`default-h1013-113445`→`113726`→…），孤儿 key 堆积。
        逐账号合并（读最新基线 → merge → save_snapshot），崩溃最多丢当前 1 个。
        """
        try:
            # 🔴 2026-10-18：加**重试** —— 台账快照是共享文件，regloop 的
            #    run.py、猎手、捡漏可能同时读写；并发下 load/save 会撞
            #    （实测探针崩在 _flush_one 的 save_snapshot）。重试 3 次、
            #    间隔 0.5s，撞完再写；仍失败就走批末兜底（外层已 except）。
            _last = None
            for _try in range(3):
                try:
                    _now = ledger.load_existing(ledger.ledger_path())
                    _base = _now if len(_now) > len(existing) else existing
                    merged, _, _, _ = ledger.merge_records(_base, [rec])
                    ledger.save_snapshot(merged)
                    return
                except Exception as ex:                           # noqa: BLE001
                    _last = ex
                    import time as _t
                    _t.sleep(0.5 * (_try + 1))
            raise _last
        except Exception as ex:                                   # noqa: BLE001
            log(f"⚠ 单账号落盘失败（{type(ex).__name__}: {str(ex)[:80]}）"
                f"—— 批末还有一次兜底写回")

    def work(i: int, d: dict) -> None:
        tag = f"[{i + 1}/{n}] {d.get('email', '?')[:34]}"
        def log(msg: str):
            with lock:
                print(f"{tag} {msg}", flush=True)
        try:
            # 🔴 `{**d, **out}` 而不是直接 `out`：台账写回是**按 email 整条替换**的
            #    （见 src/ledger.merge_records）。若只交增量字段，一旦这条胜出
            #    就会把 username/password/api_key 全洗掉。交超集才安全 ——
            #    无论哪一方胜出，信息都不减少。
            results[i] = {**d, **run_one(d, headless=args.headless,
                                         create=(i < args.create),
                                         rekey=args.rekey,
                                         key_name=args.key_name, log=log)}
        except Exception as ex:                                   # noqa: BLE001
            results[i] = {**d, "downstream": f"crash: {str(ex)[:160]}"}
            log(f"✗ 崩溃：{str(ex)[:140]}")
        else:
            # 🔴 有结果就立即落盘（含崩溃兜底 dict）—— 理由见 `_flush_one`。
            if results[i]:
                _flush_one(results[i])

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for f in as_completed([ex.submit(work, i, d) for i, d in enumerate(targets)]):
            f.result()
    wall = time.time() - t0
    results = [r for r in results if r]

    # ── 汇总 ──────────────────────────────────────────────────
    def cnt(key, pred=lambda v: True):
        return sum(1 for r in results if pred(r.get(key)))

    ok_login = cnt("login_ms")
    ok_key = cnt("key_id")
    ok_ver = cnt("verify", lambda v: isinstance(v, str) and v.startswith("ok"))
    no_key = cnt("verify", lambda v: v == "skipped: no plaintext key")
    pending = cnt("verify", lambda v: isinstance(v, str) and v.startswith("not active"))

    print("\n" + "=" * 72)
    print(f"下游全链路  {n} 账号 / 并发 {args.workers}  wall={wall:.1f}s")
    print(f"  登录      {ok_login}/{n}")
    print(f"  只读额度  {cnt('credits')}/{n}")
    print(f"  建/复用Key {ok_key}/{n}")
    print(f"  真实推理  {ok_ver}/{n}"
          + (f"（{no_key} 个无明文 key 跳过）" if no_key else "")
          + (f"（{pending} 个新建 key 传播未到位）" if pending else ""))

    # ── 额度分布：上一轮发现过非 10 的取值，这里必须显式列出来 ──
    creds = [r.get("credits") for r in results if r.get("credits")]
    if creds:
        uniq = sorted(set(creds))
        print(f"\n  credits 分布：{uniq}")
        # 把每个窗口的"已用/剩余"摊开 —— `available_credits` 只是各窗口的
        # 最小值，光看它分不清是哪个窗口被消耗了。
        print(f"  {'账号':38s} {'5h 已用/剩余':>18s} {'7d 已用/剩余':>18s}  available")
        for r in results:
            b = r.get("balance_raw") or {}
            w = b.get("usage_windows") or {}
            # ⚠ `w=w` 是**显式绑定**，不是冗余：`fmt` 是闭包，若不绑定就会捕获
            #   循环变量 `w` 的**引用**。当前是在同轮内立即调用（所以行为正确），
            #   但一旦有人把 `fmt` 存起来延后调用，全部窗口会变成最后一条记录的。
            def fmt(k, w=w):
                x = w.get(k) or {}
                if not x:
                    return "-"
                return f"{x.get('used_credits')}/{x.get('remaining_credits')}"
            print(f"  {r['email']:38s} {fmt('5h'):>18s} {fmt('7d'):>18s}"
                  f"  {b.get('available_credits')}")

        odd = [r for r in results if r.get("credits") not in ("10.000000", "10")]
        if odd:
            print(f"\n  ⚠ {len(odd)} 个账号 credits ≠ 10（原始 balance 已存进"
                  f" exports/balance_dump.json）")

    # 原始 balance 落盘：只读接口拿到的，是排查"额度去哪了"的唯一证据。
    # 追加式保存（按 email 覆盖同一条），避免每次跑都丢上一次的快照。
    if any(r.get("balance_raw") for r in results):
        dump_p = (ROOT / ".workbuddy-ai" / "exports" / "balance_dump.json")
        dump_p.parent.mkdir(parents=True, exist_ok=True)
        old = {}
        if dump_p.is_file():
            try:
                old = json.loads(dump_p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                old = {}
        for r in results:
            if r.get("balance_raw"):
                old[r["email"]] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                   "balance": r["balance_raw"]}
        dump_p.write_text(json.dumps(old, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"\n  balance 原始快照已存 {dump_p}（累计 {len(old)} 个账号）")

    failed = [r for r in results if r.get("downstream") != "ok"]
    if failed:
        print(f"\n  未走通 {len(failed)} 条：")
        for r in failed:
            print(f"    {r['email']:38s} {r.get('downstream')}")

    # ── 写回台账（合并，不覆盖）──────────────────────────────
    if args.no_write:
        print("\n（--no-write：未写台账）")
        return 0

    # 🔴 并发安全：批次运行期间台账可能被**别的写者**追加过记录（本地注册循环
    #    与收割工具会同时跑）。用**当下最新**的台账做合并基线，而不是批次开始时
    #    的 `existing` —— 否则 `merged` 会比当前读源少，`save_snapshot` 的
    #    `_guard_no_shrink` 直接抛 ValueError，本批结果白跑（重试又可能再撞）。
    _now = ledger.load_existing(ledger.ledger_path())
    _base = _now if len(_now) > len(existing) else existing
    merged, kept, added, upgraded = ledger.merge_records(_base, results)
    # 格式与 `run.py` 共用（src/cli.py）。
    # ⚠ 这里**没有** `if kept:` 守卫，`run.py` 有 —— 那是刻意的差异，
    #   所以共用的是**文本**，不是"打印"这个动作。
    print(_cli.merge_summary_line(kept, added, upgraded, len(merged)))
    # 🔴 2026-09-20 修：`--out` 原来是**解析了但没人用**的（写盘写死在
    #    `src_path`）。参数被忽略是最难发现的一类缺陷 —— help 里承诺了、
    #    实际不生效，而使用者只会觉得"我明明指定了路径"。
    dest = Path(args.out)
    try:
        # 🔴 判据是 `is_ledger_path(dest)`（"路径在不在台账目录里"），
        #    **不是** `run.py` 那边的 `out is None`。两者在
        #    "显式 `--out` 指向 ledger/ 内部" 这一种输入下**行为不同**：
        #    本工具会落快照，`run.py` 会走纯导出。
        #    这是**有意的**，别"顺手统一"—— 统一会悄悄改掉一边已承诺的语义。
        if ledger.is_ledger_path(dest):
            # 目标在台账目录里 ⇒ 走台账目录：留一份日期/时间戳快照（它随即成为
            # **新读源**），并把本次结果刷进 `latest.json`。
            snap, last = ledger.save_snapshot(merged)
            written = snap
            print(f"台账已更新：\n  快照（读源） {snap}\n  本批结果     {last}")
        else:
            written = ledger.save(dest, merged)
            print(f"台账已更新 {written}")
    except ValueError as ex:
        print(f"✗ {ex}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
