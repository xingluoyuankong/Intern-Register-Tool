"""一次登录尝试：8 个步骤 + 结果构造 + 编排。

切点不是新发明的 —— 来自原 `_run_attempt` 内部注释的编号 `1)~8)`
与 `mark()` 埋点（见 docs/refactor-14-browser-split-plan.md §1.4）。

分层：本模块依赖 constants / state / behavior / captcha / urls，
**不被它们反向依赖**（`session` 才用 `_run_attempt`）。
"""

import random
import time

import re
import requests

from .. import config
from .behavior import _idle_wait, _micro_move, _warmup_mouse
from .captcha import _click_checkbox, _has_slider
from .constants import (
    ANTI_DETECT_JS,
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
    page.route("**/*", route_handler)

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
            tab = page.get_by_text("密码登录", exact=True).first
            tab.wait_for(state="visible", timeout=UI_WAIT_MS)
            try:
                tab.click(timeout=8000)
            except Exception:
                tab.click(timeout=8000, force=True)
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


def _step_type_credentials(page, st: _AttemptState, acc_box, account: str,
                           password: str) -> None:
    """逐字输入账号与密码。"""
    # 3) 逐字输入（用 type 而非 fill —— fill 不产生任何键盘事件）。
    #    ⚠ 按键间隔见 `TYPE_DELAY_LO/HI` 的说明：默认值来自人类打字的量级，
    #      但"能不能调小"必须实测，不要当成铁律。
    acc_box.click(timeout=10000)
    page.keyboard.type(account, delay=random.randint(TYPE_DELAY_LO, TYPE_DELAY_HI))
    page.wait_for_timeout(random.randint(180, 420))
    pwd_box = page.locator("#normal_login_password")
    pwd_box.click(timeout=10000)
    page.keyboard.type(password, delay=random.randint(TYPE_DELAY_LO, TYPE_DELAY_HI))
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
                          verbose: bool) -> tuple:
    """点复选框（最多 `MAX_CLICKS_PER_ATTEMPT` 轮），返回新光标位置。"""
    cap = st.cap
    # 🔴 2026-10-05：T001 无感通过后，交互式「确认您不是机器人」浮层是**延迟出现**
    #    的（实测约 5~12s 才渲染出 `#aliyunCaptcha-checkbox-icon`）。而
    #    `_click_checkbox` 里 `bounding_box` 对"存在但不可见"会**立刻返回 None**
    #    （不等待），于是紧接着 T001 就去点会瞬间失败。必须先**轮询等它可见**。
    deadline = time.time() + 25
    while time.time() < deadline and not st.jwt:
        try:
            icon = page.locator("#aliyunCaptcha-checkbox-icon").first
            box = icon.bounding_box(timeout=1000)
            if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)

    # 8) 点击复选框，最多 MAX_CLICKS_PER_ATTEMPT 轮
    for attempt in range(MAX_CLICKS_PER_ATTEMPT):
        if st.jwt:
            break
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
        for _ in range(10):
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


def _block_heavy_resources(route):
    """掐掉图片/字体/媒体，只放行 HTML/JS/CSS/XHR。"""
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
    if headless:
        # 唯一的 UA 覆盖：去掉 HeadlessChrome 自我标记，版本号原样保留。
        # Chrome 的 reduced UA 只用主版本号（Chrome/152.0.0.0），
        # 而 browser.version 是完整版本（152.0.7977.83），故取首段。
        major = (browser.version or "").split(".")[0]
        if major:
            ctx_kwargs["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{major}.0.0.0 Safari/537.36"
            )
    ctx = browser.new_context(**ctx_kwargs)
    ctx.add_init_script(ANTI_DETECT_JS)
    page = ctx.new_page()
    # 🔴 走代理时页面的图片/字体/媒体是纯负担：既拖慢加载（实测 goto 120s
    #    都超时），又放大被 WAF 拦的概率（请求数越多越像爬虫）。
    #    登录只依赖 HTML + JS（验证码脚本），其余一律掐掉。
    page.route("**/*", _block_heavy_resources)
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

        if screenshot_prefix:
            page.screenshot(path=f"{screenshot_prefix}{tag}_filled.png")

        _step_submit(page, st)
        cur, waited = _step_wait_captcha(page, st, timeout=timeout, vw=vw, vh=vh,
                                         cur=cur, verbose=verbose)
        _step_click_until_jwt(page, st, vw=vw, vh=vh, cur=cur, verbose=verbose)

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
