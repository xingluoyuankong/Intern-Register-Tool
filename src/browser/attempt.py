"""一次登录尝试：8 个步骤 + 结果构造 + 编排。

切点不是新发明的 —— 来自原 `_run_attempt` 内部注释的编号 `1)~8)`
与 `mark()` 埋点（见 docs/refactor-14-browser-split-plan.md §1.4）。

分层：本模块依赖 constants / state / behavior / captcha / urls，
**不被它们反向依赖**（`session` 才用 `_run_attempt`）。
"""

import random
import time

import json
import re
import requests

from .. import config
from .behavior import (_idle_wait, _micro_move, _pre_submit_human_session,
                      _warmup_mouse)
from .captcha import _click_checkbox, _has_slider
from .constants import (
    ANTI_DETECT_JS,
    CAPTCHA_HARD_REJECT,
    FIREFOX_ANTI_DETECT_JS,
    MAX_CLICKS_PER_ATTEMPT,
    MICRO_BUDGET_S,
    MICRO_MOVE,
    PREWARM_MS,
    TYPE_DELAY_HI,
    TYPE_DELAY_LO,
    UI_WAIT_MS,
)
from .state import LoginResult, _AttemptState
from .urls import build_login_url


class _EntryNotRendered(Exception):
    """SPA 登录入口（账号/密码登录）经多次重试 + 表单直填兜底后仍渲染不出。

    仅用于把"入口没渲染"这一失败原因干净地抛到 `_run_attempt` 的 except
    分支，进而写进 `LoginResult.reason` / `timings` —— 不新增 LoginResult
    字段（`reason` 是自由文本、`timings` 是自由字典，足以承载诊断信息）。
    """


# ────────────────────────────────────────────────────────────────
# 一次尝试的 8 个步骤
# 切点不是我发明的 —— 来自原注释里的编号 `1)~8)` 与 mark() 埋点
# ────────────────────────────────────────────────────────────────
# 🔴 2026-10-02：登录页 SPA 的静态资源（`/static/js/*.chunk.js` 等）
#    **浏览器直连会被 `ERR_CONNECTION_RESET`**，而 curl/requests 打同样 URL
#    能拿到（http=200，只是慢 ~20s）。浏览器拉不到主 JS → SPA 渲染不出
#    「使用手机号 / 密码登录」入口 → 登录卡死。改由 requests 抓取后经
#    `route.fulfill` 喂回浏览器。按 URL 缓存，避免每次重复拉。
_STATIC_CACHE: dict = {}


def _fetch_static(url: str, timeout: int = 60) -> tuple:
    """用 requests 直连抓 sso 静态资源，返回 `(content_type, body)`。

    🔴 WAF 挑战页污染（2026-10-03 本机实测）：裸 requests 直连偶发被
       acw 挑战 —— 响应 200 + HTML（`<script>arg1='…'`），若原样
       fulfill 给浏览器，SPA 主 JS 就变成挑战页 → 「使用手机号 /
       密码登录」入口永不渲染 → 登录 8s/120s 超时。
       修复两层：
       ① 检测挑战页 → 就地算 `acw_sc__v2` cookie 重放一次（与 sso.py
          同款算法）；
       ② 只缓存**确认非挑战**的结果，挑战响应绝不进缓存。
    """
    if url in _STATIC_CACHE:
        return _STATIC_CACHE[url]
    sess = requests.Session()
    # 关 trust_env：本机 HTTP_PROXY/HTTPS_PROXY 会把请求带进 Clash，
    # 而 Clash 到 sso 走不通（实测代理 000）。
    sess.trust_env = False
    r = sess.get(url, timeout=timeout)
    r.raise_for_status()

    def _is_challenge(resp) -> bool:
        ct = (resp.headers.get("Content-Type") or "").lower()
        if "javascript" in ct or "json" in ct:
            return False
        head = resp.text[:600]
        return "acw_sc" in head and "arg1=" in head

    if _is_challenge(r):
        # 挑战页：从 arg1 算 cookie，带 cookie 重放（算法与 SSOClient 同款）
        m = re.search(r"arg1='([0-9A-Fa-f]{40})'", r.text)
        if m:
            from ..sso import SSOClient

            sess.cookies.set("acw_sc__v2", SSOClient._acw_sc_v2(m.group(1)),
                             domain="sso.openxlab.org.cn", path="/")
            r = sess.get(url, timeout=timeout)
            r.raise_for_status()
        if _is_challenge(r):
            # 重放仍被挑战 → 当失败处理，让调用方 continue_ 走直连
            raise RuntimeError("static fetch stuck in WAF challenge")

    ct = r.headers.get("Content-Type", "application/javascript")
    _STATIC_CACHE[url] = (ct, r.content)
    return _STATIC_CACHE[url]


def _trim_login_factory(st: _AttemptState):
    """构造登录页请求路由：掐重资源 + 同源 JS 走 requests 回填。

    🔴 2026-10-04 放宽匹配（P0-1）：原实现只对 `/static/` 下的同源资源回填，
       但 SPA 的 chunk 名/路径可能不含 `/static/`（哈希文件名、`/assets/` 等），
       漏掉的同源 JS 一旦浏览器直连被 RESET，入口就永不渲染 → 登录 120s 超时。
       这里放宽成：**同源（`config.SSO_BASE`）的 document/script/xhr/fetch**
       一律走 `_fetch_static` 回填，其余非必要资源 abort。
       ⚠ stylesheet 仍放行（不能 abort）—— 登录页靠 CSS 决定元素可见性。
    """

    def _trim_login(route):
        u = route.request.url
        rt = route.request.resource_type
        if rt in ("image", "font", "media"):
            route.abort()
            return
        # 🔴 2026-10-05 回退：**只回填 `/static/` 静态资源**，绝不拦同源
        #    xhr/fetch/document。原因：`_fetch_static` 用的是**无 Cookie 的独立
        #    requests**，拿它回填同源 XHR 会让 SPA 收到"无会话"的响应 →
        #    验证码浮层/登录表单初始化失败（实测页面报「表单验证失败」且
        #    验证码浮层永不出现）。这是 P0-1 那次"放宽匹配"引入的回归。
        if u.startswith(config.SSO_BASE) and "/static/" in u:
            try:
                ct, body = _fetch_static(u)
                route.fulfill(status=200, content_type=ct, body=body)
            except Exception:
                route.continue_()
        else:
            route.continue_()

    return _trim_login


def _step_open_form(page, st: _AttemptState):
    """导航到登录页 → 切"账号登录" → 切"密码登录" tab，直到输入框就绪。

    返回 `acc_box`（账号输入框 locator），供 `_step_type_credentials` 复用。

    🔴 2026-10-04（P0-1）：加**分级兜底**，因为实测 42 次 `TimeoutError` 都死在
       「使用手机号 / 密码登录」入口永不 `visible`。三层：
         ① 正常路径：等入口可见 → 点它 → 切密码 tab → 等输入框。
         ② 若入口始终不可见，但**输入框已存在**（表单可能已在密码 tab 或
            直达渲染）→ 跳过入口/tab 点击，直接返回输入框。
         ③ 首屏失败则 `goto` 重试（重触发路由回填），最多 3 次。
    """
    route_handler = _trim_login_factory(st)
    # 路由**只挂一次**（重复挂同一 handler 会叠加，unroute 也难清理干净）；
    # 重试只重跑 goto，靠重新发起的请求再次命中路由回填。
    # 🔴 2026-10-17：route 协议失配降级（playwright 1.64 / camoufox 0.5.8，
    #    见 launch.route_safe）—— 挂不上就直连，登录慢一点但流程完整。
    # 🔴 2026-10-18 根因修正：route 在**导航前挂载**必失败（协议错误），
    #    导航后挂载完全正常。用 route_after_nav 先推 about:blank 再挂，
    #    让首个真实 goto 的静态资源回填从头生效 —— 否则 SPA 主 JS 直连
    #    被 RESET，表单渲染不出（实测 captcha init=0 / open_form 40s 超时）。
    from .launch import route_after_nav
    route_after_nav(page, "**/*", route_handler)

    def _once():
        page.goto(build_login_url(), wait_until="domcontentloaded",
                  timeout=120000)
        st.mark("goto")
        if PREWARM_MS > 0:
            page.wait_for_timeout(PREWARM_MS)
            st.mark("prewarm")

        acc_box = page.locator("#normal_login_account")

        # ① 正常路径：入口 → 密码 tab → 输入框
        try:
            entry = page.get_by_text("使用手机号 / 密码登录",
                                     exact=False).first
            entry.wait_for(state="visible", timeout=UI_WAIT_MS)
            try:
                entry.click(timeout=8000)
            except Exception:
                entry.click(timeout=8000, force=True)
            # 🔴 2026-10-18 关键修复（真机取证）：
            #    点完入口后默认停在 **「验证码登录」**（手机验证码）tab，
            #    页面文案实测为「验证码登录 | 密码登录」。旧的 `exact=True`
            #    找不到「密码登录」就静默超时，于是**在手机验证码表单里
            #    填邮箱+密码**，提交后服务端只回 T001 —— 看起来像验证码
            #    被拒，实际是登录方式选错了。
            #    ⇒ 必须显式点「密码登录」tab，并用非 exact 匹配兜住
            #      文案差异（空格/箭头等），点完再确认表单已切换。
            page.wait_for_timeout(random.randint(300, 700))
            tab = None
            for sel in (page.get_by_text("密码登录", exact=True),
                        page.get_by_text("密码登录", exact=False)):
                try:
                    cand = sel.first
                    cand.wait_for(state="visible", timeout=6000)
                    tab = cand
                    break
                except Exception:
                    continue
            if tab is not None:
                try:
                    tab.click(timeout=8000)
                except Exception:
                    try:
                        tab.click(timeout=8000, force=True)
                    except Exception:
                        pass
                st.ev("pwd_tab_clicked", "密码登录")
                page.wait_for_timeout(random.randint(200, 500))
            else:
                st.ev("pwd_tab_missing", "未找到密码登录 tab")
            acc_box.wait_for(state="visible", timeout=UI_WAIT_MS)
        except Exception as exc:      # noqa: BLE001 落到②兜底
            # ② 兑底：入口不可见，但输入框可能已在（password tab 默认或直达）。
            st.ev("open_form_fallback", f"entry/tab failed: {type(exc).__name__}")
            try:
                acc_box.wait_for(state="visible", timeout=15000)
            except Exception:
                raise exc            # 两层都不行 → 上抛给③重试

        page.wait_for_timeout(random.randint(120, 300))
        st.mark("form_ready")
        return acc_box

    # ③ 分级重试：首屏渲染失败（常见于静态资源回填对不上）→ 重新 goto
    last_exc = None
    for i in range(3):
        try:
            return _once()
        except Exception as exc:      # noqa: BLE001
            last_exc = exc
            st.ev("open_form_retry", f"#{i + 1} {type(exc).__name__}")
            page.wait_for_timeout(1000)
    raise last_exc


def _type_like_human(page, text: str, *, label: str = "") -> None:
    """逐字符输入，间隔按**人类按键间隔分布**生成（非均匀随机）。

    🔴 2026-10-18（F001 行为风控攻坚）：
       旧实现 `keyboard.type(text, delay=randint(45,110))` 让**每个按键
       间隔都落在同一均匀区间** —— 真人不是这样：按键间隔是**长尾分布**
       （中位 ~55~85ms，但约 8~15% 的键之间有 150~420ms 的"找键/思考"停顿，
       偶尔还有一次 >600ms 的分神）。均匀分布在这类节奏上极易被分类器
       区分（熵相同但形状错）。这里直接按混合分布生成逐键间隔。
       同时保留 2% 概率的"回删一个字符"（真人打错很常见）——
       这是**真实行为**，且能显著改变按键序列特征。
    """
    for idx, ch in enumerate(text):
        if random.random() < 0.12:                 # 长尾：思考/找键停顿
            wait = random.randint(150, 420)
            if random.random() < 0.06:             # 少数分神
                wait = random.randint(550, 900)
        else:
            wait = int(random.gauss(72, 22))       # 主体：快而集中
            # 🔴 上界放宽到 2 倍 HI：真人主体区间也有 120~180ms 的慢键，
            #    死卡 110 会把长尾截断，分布形状又变回"均匀"
            wait = max(TYPE_DELAY_LO, min(wait, TYPE_DELAY_HI * 2))
        # 单词/分段边界略慢一点（真人换段时有轻微停顿）
        if ch in "@._-+":
            wait = int(wait * random.uniform(1.3, 2.0))
        if idx:
            page.wait_for_timeout(wait)
        page.keyboard.type(ch)
        # 2% 概率打错后回删（真人行为，且改变按键序列）
        if random.random() < 0.02:
            page.keyboard.press("Backspace")
            page.wait_for_timeout(random.randint(60, 180))
            page.keyboard.type(ch)


def _step_type_credentials(page, st: _AttemptState, acc_box, account: str,
                           password: str) -> None:
    """逐字输入账号与密码。"""
    # 3) 逐字输入（用 type 而非 fill —— fill 不产生任何键盘事件）。
    #    ⚠ 按键间隔见 `TYPE_DELAY_LO/HI`：这两个常量现在是**上下界**，
    #      实际间隔由 `_type_like_human` 按人类混合分布生成（2026-10-18）。
    acc_box.click(timeout=10000)
    page.wait_for_timeout(random.randint(80, 220))
    _type_like_human(page, account, label="account")
    page.wait_for_timeout(random.randint(180, 420))
    pwd_box = page.locator("#normal_login_password")
    pwd_box.click(timeout=10000)
    page.wait_for_timeout(random.randint(80, 220))
    _type_like_human(page, password, label="password")
    page.wait_for_timeout(random.randint(220, 520))
    st.mark("typed")


def _step_accept_agreements(page, st: _AttemptState) -> None:
    """勾选全部协议类复选框。"""
    # 4) 勾选全部协议类复选框
    for i in range(page.locator("input[type=checkbox]").count()):
        el = page.locator("input[type=checkbox]").nth(i)
        try:
            if not el.is_checked():
                el.check(timeout=5000)
        except Exception:
            pass
    st.mark("checkbox")


def _step_warmup(page, st: _AttemptState, vw: int, vh: int) -> tuple:
    """提交前的短暖场（2~3 轮），返回新光标位置。"""
    # 5) 短暖场（2~3 轮）。长时间鼠标活动改在下面等验证码时做
    cur = _warmup_mouse(page, vw, vh, stats=st.mv)
    st.mark("warmup")
    return cur


def _step_submit(page, st: _AttemptState) -> None:
    """点登录按钮。"""
    # 6) 提交
    page.locator("button").filter(has_text="登录").first.click(timeout=10000)
    st.ev("submit")


def _self_byaccount_submit(page, st: _AttemptState, *, verbose: bool) -> None:
    """🔴 2026-10-19：用已捕获的验证码参数**自己直接提交 login/byAccount**。

    背景：T001 通过后，本该弹出的「确认您不是机器人」浮层现在根本不渲染
    （10-18 起），前端 therefore 永远不发 byAccount。我们自己 fetch。
    🔴 时效：certifyId 是秒级一次性票据 —— 必须在 Verify 通过后**立刻**
       提交（实测晚 100s 必失效）。所以本函数在 `_step_wait_captcha`
       的 path_a 分支（通过后 ~1s）就会被调用。
    """
    page.wait_for_timeout(500)          # 给服务端 Verify 落库半秒
    try:
        from ..crypto_rsa import encrypt_password
        IDENT_ACC = st.account_ident
        PASSWORD_ACC = st.account_pwd
        if not IDENT_ACC:
            st.ev("self_byaccount_err", "no-ident")
            return
        _enc = encrypt_password(IDENT_ACC, PASSWORD_ACC)
        # 优先用 SDK 官方成功回调的完整 cvp（截获自 initAliyunCaptcha
        # success），重建版缺 captchaOutput 时后端会打 A0101。
        _cvp = ""
        _scene = st.cap.get("scene_id", "")
        try:
            _cvp = page.evaluate("window.__oh_cvp || ''")
            _scene = page.evaluate("window.__oh_scene || ''") or _scene
        except Exception:
            pass
        if not _cvp:
            _cvp = st.cap.get("cvp", "")
        if not _cvp:
            st.ev("self_byaccount_err", "no-cvp")
            return
        _js = """async (a) => {
            const r = await fetch('/gw/uaa-be/api/v1/login/byAccount', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'captchaVerifyParam': a.cvp,
                    'sceneId': a.sceneId,
                    'prefix': 'lvtb1n',
                },
                body: JSON.stringify(a.body),
            });
            return {status: r.status, text: await r.text()};
        }"""
        _body = {"account": IDENT_ACC, "password": _enc,
                 "type": ("1" if "@" in IDENT_ACC else "2"),
                 "marketing": "",
                 "remember": True}
        _r = page.evaluate(_js, {"cvp": _cvp, "sceneId": _scene,
                                 "body": _body})
        st.ev("self_byaccount",
              f"status={_r.get('status')} "
              f"body={(_r.get('text') or '')[:120]}")
        if verbose:
            print(f"    [login] 自包 byAccount -> {_r.get('status')} "
                  f"{(_r.get('text') or '')[:140]}", flush=True)
    except Exception as ex:
        st.ev("self_byaccount_err", f"{type(ex).__name__}")
        if verbose:
            print(f"    [login] 自包 byAccount 失败: {type(ex).__name__}",
                  flush=True)


def _step_wait_captcha(page, st: _AttemptState, *, timeout: int, vw: int, vh: int,
                       cur: tuple, verbose: bool) -> tuple:
    """等验证码 SDK 出结果，返回 `(新光标位置, 等待毫秒数)`。"""
    cap = st.cap
    # 7) 等验证码出结果。
    #    两条可能的通路（哪个先到算哪个）：
    #      Path A  TRACELESS 预检**自己通过**（Verify#1 = T001）→ 0 次点击
    #      Path B  TRACELESS 被拒（F001）→ Init#2 降级 CHECK_BOX → 走点击
    #    ⚠ 循环条件用**真实时间**而非计数器：`_micro_move` 单次耗时
    #      随轨迹长度浮动（~150~350ms），用 `waited += 250` 计数的话
    #      实验组会比真实时间慢 8% 左右，两组就不可比了。
    #    🔴 `MICRO_BUDGET_S` 之后转入安静期（`_idle_wait`）：见常量处的
    #      对照实验数据 —— 一直喂数据会让 TRACELESS 迟迟不认输，
    #      安静下来才能逼它下降级决定，把最坏情况钉住。
    t_cap = time.time()
    while (time.time() - t_cap) < timeout and cap["init"] < 2:
        if st.jwt:
            break
        # 🔴 Path A：无感验证（TRACELESS）**自己通过** —— 判据是
        #    `VerifyCaptchaV3` 返回 ok 且 init 仍是 1（不会降到 2）。
        #    此时 SDK 已放行，再死等 `init >= 2` 永远等不到
        #    （2026-10-02 实测：代理环境下 g.alicdn 可达 → T001 无感通过
        #    → 卡满 timeout 150s）。提前退出，交给 `_build_result` 收 jwt/cookie。
        if cap["init"] >= 1 and cap["last_ok"]:
            st.ev("captcha_path_a", "traceless passed, no downgrade")
            # 🔴 2026-10-19：**立刻**自包 byAccount（certifyId 秒级时效，
            #   等 100s 就是废纸 —— 这就是此前 A0101 的真因）。
            _self_byaccount_submit(page, st, verbose=verbose)
            break
        if (time.time() - t_cap) < MICRO_BUDGET_S:
            cur = _micro_move(page, cur, vw, vh, stats=st.mv)
        else:
            if not st.mv.get("budget_logged"):
                st.mv["budget_logged"] = True
                st.ev("micro_budget_exhausted", f"{MICRO_BUDGET_S}s")
                if verbose:
                    print(f"    [login] 微移动预算用尽（{MICRO_BUDGET_S}s），"
                          f"转入安静期等 SDK 降级", flush=True)
            _idle_wait(page, stats=st.mv)
    waited = round((time.time() - t_cap) * 1000)
    st.mark("captcha_ready")
    st.ev("captcha_ready", f"waited={waited}ms init={cap['init']}")
    if verbose:
        print(f"    [login] init count = {cap['init']} "
              f"(waited {waited / 1000:.1f}s, traceless_reject={cap['trivial']}, "
              f"micro_moves={st.mv.get('moves', 0)})", flush=True)
    return cur, waited


def _step_click_until_jwt(page, st: _AttemptState, *, vw: int, vh: int, cur: tuple,
                          verbose: bool, budget_s: float = 60.0) -> tuple:
    """点复选框（最多 `MAX_CLICKS_PER_ATTEMPT` 轮），返回新光标位置。

    budget_s: 本步骤**总**时间预算（不是单次等待预算）。
    🔴 2026-10-18：外部 `login(timeout=...)` 是整次尝试的上限，本步骤
       若用固定 90s + 每轮 45s 的等待，会直接吃穿预算并触发
       `TargetClosedError: browser has been closed`（自伤，实测踩过）。
       所以所有等待都受这个总预算约束。
    """
    cap = st.cap
    hard_end = time.time() + max(10.0, budget_s)
    # 🔴 2026-10-18 重写等待策略（真机取证，见下方 DOM 时间线）：
    #    实测浮层出现时刻：t+5/10/15s **均无**，**t+25s 才渲染出**
    #    `aliyunCaptcha-window-popup` + `checkbox-icon [480,348,20,20]`。
    #    旧实现只等 40s **一次性**，随后 6 轮点击全打在浮层出现之前，
    #    坐标恒为 (294,20) 这种垃圾位置 → clicked=False ×6 → 误报 T001，
    #    而验证码其实早在 7s 就 T001 ok=True 通过了。
    #
    #    ⇒ 正确形态：**每轮点击前都重新等浮层**，而不是开头等一次；
    #      且等待时长**被总预算夹住**，绝不超时自伤。
    def _wait_icon(timeout: float, cur_pos: tuple) -> tuple:
        """等浮层可见；返回 (是否可见, 新光标位置)。

        🔴 2026-10-18：等待期间必须**持续真实鼠标活动**（`_micro_move`）——
           实测浮层在 t+25s / t+49s 才出现（波动大），若这段空等不发事件，
           行为数据断流（SDK 侧也不断被推迟），且真人此时手一定在动。
        🔴 2026-10-18 OOM 修复：每 3 个循环才发一次微移动（原来每圈都发，
           200+ 轨迹点/秒会显著抬高 FireFox 渲染进程内存，实测 Web Content
           进程被内核 OOM 杀掉 → TargetClosedError）。间隔拉长仍满足"手在动"。
        """
        end = min(time.time() + timeout, hard_end)
        pos = cur_pos
        tick = 0
        while time.time() < end and not st.jwt:
            try:
                icon = page.locator("#aliyunCaptcha-checkbox-icon").first
                box = icon.bounding_box(timeout=1000)
                if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                    return True, pos
            except Exception:
                pass
            tick += 1
            if (time.time() + 1.0) < end and MICRO_MOVE and tick % 3 == 1:
                pos = _micro_move(page, pos, vw, vh, stats=st.mv)
            else:
                page.wait_for_timeout(700)
        return False, pos

    # 首次等待给足 70s（实测最慢 t+49s 才出浮层，见上图），但受总预算夹住
    seen, cur = _wait_icon(min(70.0, budget_s * 0.75), cur)
    if seen:
        cap["icon_seen"] = True

    # 8) 点击复选框，最多 MAX_CLICKS_PER_ATTEMPT 轮
    for attempt in range(MAX_CLICKS_PER_ATTEMPT):
        if st.jwt:
            break
        # 🔴 2026-10-18 会话级拒绝早退（见 constants.CAPTCHA_HARD_REJECT）：
        #    实测风控高压期，服务端返回 F001→F008 且**从不下发复选框 UI**，
        #    此时无论等多久、点多少次都是 clicked=False。继续磨 6 轮只是
        #    白白消耗 2~4 分钟并加重风险分。出现硬拒绝码即退出，
        #    交给上层换号/冷却。
        if any(v in CAPTCHA_HARD_REJECT for v in (cap.get("verify") or [])):
            st.ev("hard_reject_abort", ",".join(cap.get("verify") or []))
            if verbose:
                print(f"    [login] 🛑 会话级拒绝（{cap['verify']}）→ 提前结束本次尝试",
                      flush=True)
            break
        # 🔴 2026-10-18 核心修复：**每轮点击前都等浮层**（不是开头等一次）。
        #    实测浮层 t+25s 才出现，开头一次性 40s 等待在有浮层的场景下够，
        #    但一旦首轮落空，后续轮次全在"浮层未出现"的状态下点空
        #    （坐标恒为 (294,20) / (663,20) 这类垃圾位置）。每轮重等一下，
        #    浮层真出现时立刻能点到。
        if attempt > 0 and not cap.get("icon_seen"):
            if time.time() < hard_end:
                got, cur = _wait_icon(30, cur)
                if got:
                    cap["icon_seen"] = True
                    st.ev("icon_visible", f"round={attempt + 1}")
        # 🔴 2026-10-05 关键修复：**不再对 Path A（T001 无感通过）走"只等不点"
        #    的捷径**。实测（本机抓包 + DOM 快照）T001 无感通过后，页面**仍然**
        #    弹出「请完成安全验证 / 确认您不是机器人」浮层（`#aliyunCaptcha-window-popup`
        #    + `#aliyunCaptcha-checkbox-icon`），**必须点这个复选框**才会触发
        #    `login/byAccount` 并写入 `uaa-token`。
        #    旧逻辑在 `init<=1 and last_ok` 时跳过点击、只等 10s → 永远拿不到 jwt，
        #    表现为 `captcha rejected (T001)`（其实验证码是过的，只是没点确认）。
        #    手动点 `#aliyunCaptcha-checkbox-icon` 后立即 `byAccount 200` + jwt 落地。
        #    `_click_checkbox` 已带 6s 短超时、拿不到框返回 False（不再炸整次尝试），
        #    所以这里**无条件尝试点击**是安全的；真没框时下面会退化成等 token。
        cur, clicked = _click_checkbox(page, cur)
        st.ev(f"click#{attempt + 1}", f"clicked={clicked}")
        if verbose:
            print(f"    [login] click #{attempt + 1} at "
                  f"({cur[0]:.0f},{cur[1]:.0f}) clicked={clicked}", flush=True)
        if not clicked:
            # 没有复选框（或还没渲染）：退化成等 token 落地（真·Path A 场景），
            # 等一小会儿再看下一轮；别像旧逻辑那样直接 break 放弃。
            #
            # 🔴 2026-10-18 关键修复（取证见 tools/probes/t001_forensics.py）：
            #    现场抓到 `VerifyCaptchaV3 -> T001 ok=True` + `init=1`
            #    = 无感验证**已通过**、且**根本不弹确认浮层**，于是 6 轮点击
            #    全部 clicked=False、光标停在 (847,20) 这种垃圾坐标，
            #    最后被判成 `captcha rejected (T001)` —— 其实验证码是过的。
            #    ⇒ 连续 2 轮点不到框 = Path A 确认：验证码已放行，
            #      直接**再提交一次登录表单**把这次验证用掉（旧逻辑只会干等）。
            if attempt >= 1:
                # 🔴 2026-10-18 重大回归修复（取证 tools/probes/t001_forensics.py）：
                #    旧逻辑无条件重交表单，实测把**本来已经通过**的验证码
                #    反复打回 F001（行为风控拒绝）。现场证据链：
                #      Init#1 → Verify T001 ok=True   ← 验证码已过
                #             → click=False(无浮层) → 重交
                #      Init#2 → Verify F001 ok=False  ← 重交触发行为风控
                #      Init#3 → click=True → T001 ok=True ← 又过了
                #             → 又重交 → Init#4 → F001 ...
                #    6 轮循环死在这个自伤动作上，最后报 captcha rejected(F001)。
                #
                #    ⇒ 重交只在「验证码**没通过**」时才允许（last_ok=False）。
                #      last_ok=True 说明 SDK 已放行，此时正确动作是**等**
                #      jwt/cookie 落地（服务端 byAccount 需要时间），
                #      或者等确认浮层出现再点，绝不重交。
                if not cap["last_ok"]:
                    st.ev("path_a_direct_submit", "no checkbox & not passed → resubmit")
                    if verbose:
                        print("    [login] 无浮层且未通过 → 重交表单", flush=True)
                    try:
                        _step_submit(page, st)
                    except Exception as ex:
                        st.ev("submit_err", f"{type(ex).__name__}")
                        if verbose:
                            print(f"    [login] 重交失败: {type(ex).__name__}",
                                  flush=True)
                else:
                    # 已验证通过却无浮层：等 jwt（不重交，避免触发 F001）
                    st.ev("path_a_wait_jwt", "passed, no checkbox → wait")
                    if verbose:
                        print("    [login] 验证码已通过、无浮层 → 等 jwt（不重交）",
                              flush=True)
                    # 🔴 2026-10-19 自包 byAccount：浮层现在根本不弹（10-18
                    #    起），但 VerifyCaptchaV3 已返回 T001 ok=True ——
                    #    CaptchaVerifyParam（certifyId+deviceToken）就在手里。
                    #    前端提交链（main.js Xe）：POST /gw/uaa-be/api/v1/
                    #    login/byAccount，headers={captchaVerifyParam,
                    #    sceneId, prefix:"captcha"}，body={account, password:
                    #    RSA("account||pwd"+秒), type}。自己 fetch 把这次
                    #    验证用掉 —— 短等 8s 仍无 jwt 才走这条路。
                    for _w in range(8):
                        page.wait_for_timeout(1000)
                        if st.jwt:
                            break
                    if not st.jwt and cap.get("cvp"):
                        # 🔴 2026-10-19 取证：浮层 DOM 在不在（隐藏≠没有，
                        #    且可能在 iframe 里 —— 顶级 locator 根本看不到）。
                        try:
                            _dom = page.evaluate("""() => {
                                const out = {frames: [], top: {}};
                                for (const f of document.querySelectorAll('iframe')) {
                                    out.frames.push({src: (f.src||'').slice(0,80),
                                        w: f.clientWidth, h: f.clientHeight});
                                }
                                const ids = ['aliyunCaptcha-window-popup',
                                    'aliyunCaptcha-checkbox-icon',
                                    'captcha-element', 'aliyunCaptcha-btn',
                                    'aliyunCaptcha-btn-wrapper'];
                                for (const id of ids) {
                                    const el = document.getElementById(id);
                                    if (!el) { out.top[id] = 'absent'; continue; }
                                    const cs = getComputedStyle(el);
                                    out.top[id] = ((cs.display === 'none'
                                        || cs.visibility === 'hidden'
                                        || parseFloat(cs.opacity || '1') === 0)
                                        ? 'hidden' : 'visible')
                                        + ` w=${el.clientWidth} h=${el.clientHeight}`;
                                }
                                return out;
                            }""")
                            st.ev("popup_dom", json.dumps(_dom))
                            if verbose:
                                print(f"    [login] 浮层DOM: "
                                      f"{json.dumps(_dom)[:300]}",
                                      flush=True)
                        except Exception as ex:
                            st.ev("popup_dom_err", type(ex).__name__)
                        # 强点：JS 原生 click 绕过可见性限制（隐藏也能点）
                        try:
                            _fc = page.evaluate("""() => {
                                for (const id of
                                    ['aliyunCaptcha-checkbox-icon',
                                     'aliyunCaptcha-btn-wrapper',
                                     'aliyunCaptcha-btn']) {
                                    const el = document.getElementById(id);
                                    if (el) { el.click(); return id; }
                                }
                                return '';
                            }""")
                            if _fc:
                                st.ev("force_click", _fc)
                                if verbose:
                                    print(f"    [login] JS强点: {_fc}",
                                          flush=True)
                                for _w in range(8):
                                    page.wait_for_timeout(1000)
                                    if st.jwt:
                                        break
                        except Exception:
                            pass
                        try:
                            from ..crypto_rsa import encrypt_password
                            IDENT_ACC = st.account_ident
                            PASSWORD_ACC = st.account_pwd
                            _enc = encrypt_password(IDENT_ACC, PASSWORD_ACC)
                            # 🔴 优先用 SDK 官方成功回调的完整 cvp
                            #    （截获自 initAliyunCaptcha success），
                            #    重建版缺 captchaOutput 会被 A0101 打回。
                            _cvp = ""
                            _scene = cap.get("scene_id", "")
                            try:
                                _cvp = page.evaluate("window.__oh_cvp || ''")
                                _scene = page.evaluate(
                                    "window.__oh_scene || ''") or _scene
                            except Exception:
                                pass
                            if not _cvp:
                                _cvp = cap["cvp"]
                            st.ev("self_byaccount_cvp",
                                  f"src={'sdk' if _cvp != cap['cvp'] else 'rebuilt'} "
                                  f"len={len(_cvp)}")
                            if verbose:
                                print(f"    [login] cvp 来源="
                                      f"{'SDK官方' if _cvp != cap['cvp'] else '重建'}"
                                      f" len={len(_cvp)}", flush=True)
                            _js = """async (a) => {
                                const r = await fetch('/gw/uaa-be/api/v1/login/byAccount', {
                                    method: 'POST',
                                    credentials: 'include',
                                    headers: {
                                        'Content-Type': 'application/json',
                                        'captchaVerifyParam': a.cvp,
                                        'sceneId': a.sceneId,
                                        'prefix': 'lvtb1n',
                                    },
                                    body: JSON.stringify(a.body),
                                });
                                return {status: r.status, text: await r.text()};
                            }"""
                            _body = {"account": IDENT_ACC, "password": _enc,
                                     "type": ("1" if "@" in IDENT_ACC else "2"),
                                     "marketing": ""}
                            _r = page.evaluate(_js, {
                                "cvp": _cvp, "sceneId": _scene,
                                "body": _body})
                            st.ev("self_byaccount",
                                  f"status={_r.get('status')} "
                                  f"body={(_r.get('text') or '')[:120]}")
                            if verbose:
                                print(f"    [login] 自包 byAccount -> "
                                      f"{_r.get('status')} "
                                      f"{(_r.get('text') or '')[:140]}",
                                      flush=True)
                        except Exception as ex:
                            st.ev("self_byaccount_err", f"{type(ex).__name__}")
                            if verbose:
                                print(f"    [login] 自包 byAccount 失败: "
                                      f"{type(ex).__name__}", flush=True)
                        # fetch 成功时 on_response 会捕到 authorization 头
                        for _w in range(10):
                            page.wait_for_timeout(1000)
                            if st.jwt:
                                break
                for _ in range(8):
                    page.wait_for_timeout(1000)
                    if st.jwt:
                        break
                if st.jwt:
                    break
            for _ in range(6):
                page.wait_for_timeout(1000)
                if st.jwt:
                    break
            if st.jwt:
                break
            page.wait_for_timeout(800)
            continue

        # 等本轮结果（必须 pump 事件）。同时继续做小幅移动，
        # 让"正在等待"这件事本身也表现为真人行为。
        # 🔴 2026-10-18：受总预算约束，别把外层 timeout 吃穿。
        for _ in range(10):
            if time.time() > hard_end:
                st.ev("click_budget_exhausted", f"round={attempt + 1}")
                break
            page.wait_for_timeout(1000)
            if st.jwt:
                break
            cur = _micro_move(page, cur, vw, vh, stats=st.mv)
        if st.jwt:
            break
        sl = _has_slider(page)
        if sl:
            cap["slider"] = sl
            st.ev("slider", sl)
            if verbose:
                print(f"    [login] ⚠ 弹出二次验证: {sl}", flush=True)
            break
        page.wait_for_timeout(random.randint(400, 900))

        # 🔴 2026-10-18 行为风控加固：两轮点击之间要有「真人在场」的间隔 ——
        #    3~6 次微移动（约 10~20s，视真实节奏），而不是固定 ~10s 恒定间隔
        #    的机关枪式连点（同坐标、同节奏、从不犹豫 = 教科书反自动化特征）。
        #    首次点击除外：Path B 正常节奏就是浮层出现即点。
        if attempt + 1 < MAX_CLICKS_PER_ATTEMPT and not st.jwt:
            for _ in range(random.randint(3, 6)):
                cur = _micro_move(page, cur, vw, vh, stats=st.mv)
    st.mark("done")
    if st.jwt:
        st.ev("jwt")
    return cur


def _build_result(st: _AttemptState, *, cookies: dict, waited: int) -> LoginResult:
    """把 state 折成 `LoginResult`。

    ⚠ `captcha_stage` 的键名与形状是**下游契约**（探针读、台账存），勿改。
    """
    cap = st.cap
    jwt = st.jwt or cookies.get("uaa-token", "")
    reason = ""
    if not jwt:
        if cap["slider"]:
            reason = f"secondary captcha required ({cap['slider']})"
        elif cap["verify"]:
            reason = f"captcha rejected ({cap['verify'][-1]})"
        else:
            reason = "no jwt captured"
    return LoginResult(
        ok=bool(jwt), jwt=jwt, code=st.code, reason=reason,
        cookies=cookies,
        captcha_stage={"init": cap["init"], "verify": cap["verify"],
                       "last_ok": cap["last_ok"], "slider": cap["slider"],
                       "traceless_reject": cap["trivial"],
                       "payload": cap["payload"],
                       # 🔴 2026-10-19：自包 byAccount 的原料也带出来，
                       #    供探针/下游重放（T001 成功场景下才有值）
                       "cvp": cap.get("cvp", ""),
                       "scene_id": cap.get("scene_id", ""),
                       "events": cap["events"],
                       # Path A = TRACELESS 自过（0 点击）；Path B = 降级 CHECK_BOX
                       "path": ("A" if cap["init"] <= 1 and cap["last_ok"]
                                else ("B" if cap["init"] >= 2 else "?")),
                       "mouse": {"micro_move": MICRO_MOVE,
                                 "budget_s": MICRO_BUDGET_S,
                                 "moves": st.mv.get("moves", 0),
                                 "points": st.mv.get("points", 0),
                                 "idle_waits": st.mv.get("idle_waits", 0),
                                 "budget_exhausted": bool(
                                     st.mv.get("budget_logged"))},
                       "captcha_wait_ms": waited},
        timings=st.timings,
    )


# ────────────────────────────────────────────────────────────────
# 在给定 browser 上跑一次尝试（自带 context 生命周期）
# ────────────────────────────────────────────────────────────────
def _playwright_proxy(slot: str) -> dict:
    """把 `http://user:pass@host:port` 拆成 Playwright 需要的字段。

    Playwright/Chrome 不会解析 server URL 里的 user:pass，必须分开传，
    否则代理认证静默失效（页面加载不动 → 超时）。
    """
    from urllib.parse import unquote, urlparse

    u = urlparse(slot)
    d = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port or 80}"}
    if u.username:
        d["username"] = unquote(u.username)
    if u.password:
        d["password"] = unquote(u.password)
    return d


# 走代理时掐掉的非必要资源类型（见 _run_attempt 的说明）
_HEAVY = ("image", "font", "media")


# 🔴 2026-10-18 指纹优化：验证码 SDK 自己的 CDN 白名单。阿里云验证码的
#    静态资源（精灵图/滑块拼图素材/子模块 JS）也挂在 g.alicdn.com 上，
#    无脑掐 image 会让「正常客户端」变成「连验证码素材都不加载」的异常
#    客户端 —— 这本身就是可被采集的信号，且弹滑块时素材缺失会直接坏掉。
_CAPTCHA_CDN_HOSTS = ("g.alicdn.com", "o.alicdn.com", "aliyuncs.com")


def _block_heavy_resources(route):
    """掐掉图片/字体/媒体，只放行 HTML/JS/CSS/XHR。

    🔴 2026-10-18：验证码 SDK 的 CDN 资源全放行（见 _CAPTCHA_CDN_HOSTS）。
    """
    u = route.request.url
    if any(h in u for h in _CAPTCHA_CDN_HOSTS):
        route.continue_()
        return
    if route.request.resource_type in _HEAVY:
        route.abort()
    else:
        route.continue_()


def _run_attempt(browser, *, account: str, password: str, headless: bool,
                 timeout: int, screenshot_prefix: str = None,
                 verbose: bool = False, tag: str = "",
                 proxy: str = None, fingerprint_browser: bool = False) -> LoginResult:
    """在给定 browser 上跑一次登录尝试。

    这里**只负责编排**：建 context → 注册响应回调 → 依次调用 8 个步骤 → 折结果。
    每一步的实现见上面的 `_step_*`，状态在 `_AttemptState`。

    proxy: 🔴 传入则走代理出口。不给就直连 —— 直连会踩服务器公网 IP 的
        WAF 405/限流，登录直接 net::ERR_CONNECTION 失败（注册激活都 ok 却
        拿不到 key 的真因，见 2026-09-30 台账）。
    """
    st = _AttemptState(verbose=verbose)
    # 🔴 2026-10-19：传给自包 byAccount（见 _step_click_until_jwt）
    st.account_ident = account
    st.account_pwd = password

    # 🔴 指纹浏览器（Camoufox）：locale / 时区 / 屏幕由启动参数 `geoip=True`
    #    **自动跟随出口 IP**。这里再写死成 zh-CN + Asia/Shanghai 就会造成
    #    「美国 IP + 中国时区」这类自相矛盾的指纹 —— 反而更容易被风控识别。
    #    所以指纹浏览器下只留最小 context 参数，其余交给 Camoufox。
    if fingerprint_browser:
        # 🔴 `no_viewport=True` 不能省：Camoufox 只在 **persistent_context**
        #    分支自动补 `no_viewport`；我们用的是 `firefox.launch()`（非 persistent），
        #    而 `window=(1280,900)` 会 spoof 窗口尺寸 —— 两者冲突会让 Juggler
        #    死锁（表现为 "Sync API inside the asyncio loop" /
        #    "gBrowser never populated"）。必须显式关掉 viewport 模拟。
        ctx_kwargs = dict(color_scheme="light", no_viewport=True)
    else:
        # 不覆盖 viewport：见文件头第 7 条
        ctx_kwargs = dict(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport=None,
            color_scheme="light",
        )
    if proxy:
        # 🔴 Playwright 的 proxy 必须把认证拆成独立字段，**不能**把
        #    `user:pass@` 塞进 server URL —— 塞进去 Chrome 会静默忽略认证，
        #    表现为页面一直加载不动直到超时（2026-10-01 实测：走代理登录
        #    Page.goto 120s 超时，就是这个原因）。
        ctx_kwargs["proxy"] = _playwright_proxy(proxy)
    if headless and not fingerprint_browser:
        # 🔴 2026-10-18 限定 Chromium：下面这段 UA 归一化把 UA 写成
        #    `Chrome/xxx` —— 只该用于 Chromium 引擎。此前 Camoufox 无头时
        #    会被套上 Chrome UA（ Firefox 内核 + Chrome UA = 一眼假矛盾指纹）。
        #    唯一的 UA 覆盖：去掉 HeadlessChrome 自我标记，版本号原样保留。
        #    Chrome 的 reduced UA 只用主版本号（Chrome/152.0.0.0），
        #    而 browser.version 是完整版本（152.0.7977.83），故取首段。
        major = (browser.version or "").split(".")[0]
        if major:
            ctx_kwargs["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{major}.0.0.0 Safari/537.36"
            )
    ctx = browser.new_context(**ctx_kwargs)
    # 🔴 2026-10-18 引擎分家：`window.chrome` 补丁是 **Chromium 专属**——
    #    真实 Firefox 的 window 里没有 chrome 对象，给 Camoufox 注入等于在
    #    「全套 Firefox 指纹」上盖一个 Chrome 戳（原型链/UA/内核全是 Firefox，
    #    唯独 window.chrome 存在），属于一眼假的自爆特征。Firefox 走自己的
    #    加固脚本（只调 hardwareConcurrency 真值偏低的矛盾）。
    if fingerprint_browser:
        ctx.add_init_script(FIREFOX_ANTI_DETECT_JS)
        # 🔴 2026-10-19：截获阿里云验证码 SDK 的**官方成功回调参数**。
        #    前端 initAliyunCaptcha 的 success(e) 里 e 是完整 cvp
        #    （{sceneId,certifyId,captchaOutput,passToken,deviceToken…}），
        #    我们从 VerifyCaptchaV3 请求重建的 cvp 缺 captchaOutput 等
        #    字段 ⇒ 自包 byAccount 被 A0101 打回。截获后挂到
        #    window.__oh_cvp，由 path_a_wait_jwt 分支取用。
        ctx.add_init_script("""
            (function () {
                var _orig = window.initAliyunCaptcha;
                window.__oh_cvp = '';
                window.__oh_scene = '';
                Object.defineProperty(window, 'initAliyunCaptcha', {
                    configurable: true,
                    get: function () { return function (cfg) {
                        try {
                            var _s = cfg.success;
                            cfg.success = function (e) {
                                try {
                                    window.__oh_cvp = (typeof e === 'string')
                                        ? e : JSON.stringify(e);
                                    window.__oh_scene = cfg.SceneId ||
                                        cfg.sceneId || '';
                                } catch (err) {}
                                return _s && _s(e);
                            };
                        } catch (err) {}
                        return _orig(cfg);
                    };},
                    set: function (v) { _orig = v; },
                });
            })();
        """)
    else:
        ctx.add_init_script(ANTI_DETECT_JS)
    page = ctx.new_page()
    # 🔴 走代理时页面的图片/字体/媒体是纯负担：既拖慢加载（实测 goto 120s
    #    都超时），又放大被 WAF 拦的概率（请求数越多越像爬虫）。
    #    登录只依赖 HTML + JS（验证码脚本），其余一律掐掉。
    # 🔴 2026-10-17：route 协议失配降级（见 launch.route_safe）——
    #    挂不上就放行全部资源（慢，但登录不挂）。
    # 🔴 2026-10-18 根因修正：必须**导航后**挂（route_after_nav），
    #    否则掐资源策略整体失效，图片/字体全量加载进一步拖慢 goto。
    from .launch import route_after_nav
    route_after_nav(page, "**/*", _block_heavy_resources)
    page.on("response", st.on_response)
    # 🔴 诊断：登录页是 SPA，「使用手机号 / 密码登录」由 JS 渲染。它引用的
    #    JS 分两类：本地 /static/js/*.chunk.js（sso 域名）和外部 CDN
    #    （g.alicdn.com / o.alicdn.com / res.wx.qq.com）。任一类加载失败，
    #    SPA 就渲染不出入口，表现为 wait_for 超时却看不出原因 —— 这里打出来。
    if verbose:
        def _on_failed(req):
            try:
                err = req.failure
            except Exception:
                err = "?"
            print(f"    [net] FAILED {req.url[:130]} err={err}", flush=True)

        page.on("requestfailed", _on_failed)

    try:
        acc_box = _step_open_form(page, st)
        _step_type_credentials(page, st, acc_box, account, password)
        _step_accept_agreements(page, st)

        vw, vh = page.evaluate("[window.innerWidth, window.innerHeight]")
        cur = _step_warmup(page, st, vw, vh)

        # 🔴 2026-10-18 F001/F008 攻坚：提交前补一段**真实用户会有的页面交互**
        #    （滚动/悬停停留/误点空白/焦点切换，见 behavior._pre_submit_human_session）。
        #    阿里云 captchaVerifyParam 评估的是整段会话的行为密度，只有
        #    "微移动+打字"的会话密度远低于真人，是会话级风控的主要空白。
        #    时长随机 4~9s（真人填完表单到点登录的犹豫期），不影响功能。
        cur = _pre_submit_human_session(
            page, vw, vh, stats=st.mv,
            seconds=random.uniform(4.0, 9.0))
        st.mark("human_session")

        if screenshot_prefix:
            page.screenshot(path=f"{screenshot_prefix}{tag}_filled.png")

        _step_submit(page, st)
        # 🔴 2026-10-18：验证码等待也受剩余预算约束（原来直接吃满 timeout，
        #    导致后面的点击步骤完全没有时间，且总和可能超过外层 timeout）。
        elapsed = (time.time() - st.t_start)
        cap_budget = max(30.0, timeout - elapsed - 110.0)
        cur, waited = _step_wait_captcha(page, st,
                                         timeout=min(timeout, cap_budget),
                                         vw=vw, vh=vh,
                                         cur=cur, verbose=verbose)
        left = max(15.0, timeout - (time.time() - st.t_start) - 5.0)
        _step_click_until_jwt(page, st, vw=vw, vh=vh, cur=cur, verbose=verbose,
                              budget_s=min(110.0, left))

        if screenshot_prefix:
            page.screenshot(path=f"{screenshot_prefix}{tag}_done.png")

        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        return _build_result(st, cookies=cookies, waited=waited)
    except Exception as ex:
        try:
            if screenshot_prefix:
                page.screenshot(path=f"{screenshot_prefix}{tag}_error.png")
        except Exception:
            pass
        return LoginResult(ok=False, reason=f"{type(ex).__name__}: {ex}"[:200],
                           captcha_stage={"init": st.cap["init"],
                                          "verify": st.cap["verify"],
                                          "events": st.cap["events"]},
                           timings=st.timings)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
