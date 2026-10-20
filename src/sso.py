"""OpenXLab SSO 客户端 —— 注册、激活、登录（纯 HTTP 部分）。

接口清单（均以 /gw/uaa-be 为前缀）：
  POST /api/v1/register/check            {"item": email, "type": "email"} -> {"exist": bool}
  POST /api/v1/personal/username/check   {"username": "..."}              -> bool（true=可用）
  POST /api/v1/cipher/getPubKey          {"type": "register", "from": "browser"}
  POST /api/v1/register/byEmail          {"username","email","password","source","clientId"}
  POST /api/v1/register/active           {"token": "...", "sign": "..."}  （body 即 URL query 对象）
  POST /api/v1/login/byAccount           {"account","password","autoLogin"}  ← 强制人机验证
  POST /api/v1/internal/auth             {"clientId": "..."} -> {"code": "uaa::code::xxx"}

关键结论：
  - **注册/激活不需要人机验证**（失败时报 A0216 密码解密失败，而非 B0501 人机验证失败）
  - **登录强制人机验证**，纯 HTTP 无解，必须走 `src/browser/`（浏览器登录子包）
  - 密码字段 = RSA_PKCS1v15(f"{identity}||{password}{unix_ts}") 的 base64

🔴 429 限流的真实边界（2026-09-15 实测，见 tools/probes/probe_429.py）：
  - `personal/username/check`（只读）：**8 路并发也完全不限流**
  - `register/byEmail`（写）：**4 路并发时 3 路被 429 拒绝**，且是立即拒绝（~1.2s）
  所以限流挂在**写操作**上，不是笼统的 IP 突发限速。
  429 是限流信号而非业务错误 —— 必须退避重试，不能当注册失败处理。
  本项目实测：加退避重试 + 注册并发降到 2 之后可稳定跑通。
"""

import json
import random
import threading
import time
from dataclasses import dataclass

import requests

from . import config
from .crypto_rsa import encrypt_password


class WafChallengeError(RuntimeError):
    """撞上 WAF 挑战页，且调用方选择**放弃解题**（`abandon_on_challenge`）。

    刻意不用 `requests` 的异常类型：它表达的是"这个出口现在过不去"，
    不是"网络故障"。调用方（pipeline）按 network 失败处理 →
    `report_failed`（短冷却）→ 下一个号换出口。
    """


class RateLimitedError(RuntimeError):
    """`register/byEmail` 的限流（429）—— **按出口 IP** 记，不是业务失败。

    🔴 2026-10-13 实测：同一出口连着两次 429，而换出口立刻拿到业务响应
    （A0101 / A0216）。所以它必须走 `ERR_NETWORK`（换槽位重试），**不能**
    落进 `ERR_REJECTED` —— 后者会被 producer 当成"业务拒绝"直接 `break`，
    既不换出口也不重试，账号就这么废了。
    """


@dataclass
class RegisterResult:
    ok: bool
    sso_uid: str = ""
    email: str = ""
    username: str = ""
    msg_code: str = ""
    msg: str = ""


class _WrapResponse:
    """`WafSolver.browser_post` 结果 → 与 `requests.Response` 同形状的薄包装。

    调用方（stage_register 等）只用到 `.status_code` / `.text` / `.json()`，
    这里只实现这三个；`.headers` 给空 dict（`_is_waf_challenge` 之类不会
    再被调到——浏览器路径成功就说明已过 WAF）。
    """

    def __init__(self, res: dict):
        self.status_code = int(res.get("status", 0))
        self.text = res.get("text", "")
        self.headers: dict = {}

    def json(self) -> dict:
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}: {self.text[:120]}")


# P0-2: WAF cookie 复用缓存。
#   同一出口 IP 上，acw_sc__v2 / acw_tc 解出之后**长期有效**；但每个账号都是一个新的
#   SSOClient（池子 / 跨进程），默认每次都重开浏览器解题 ⇒ 出口越多越慢、还反复打扰。
#   这里按下层出口（proxy url）缓存解出后的 cookie，后续同出口的客户端直接种上，跳过
#   浏览器解题。命中后**仍**被挑战 ⇒ 视为失效，失效后重解并刷新缓存（见 _invalidate）。
_WAF_COOKIE_CACHE: "dict[str, list[dict]]" = {}
_waf_cache_lock = threading.Lock()


def _waf_key(proxy: str | None) -> str:
    return proxy or "__direct__"


def _cache_waf_cookies(key: str, session) -> None:
    cached = []
    for c in session.cookies:
        if c.name in ("acw_sc__v2", "acw_tc") and "openxlab" in (c.domain or ""):
            cached.append({"name": c.name, "value": c.value, "domain": c.domain})
    if cached:
        with _waf_cache_lock:
            _WAF_COOKIE_CACHE[key] = cached


def _seed_waf_cookies(key: str, session) -> bool:
    with _waf_cache_lock:
        cached = _WAF_COOKIE_CACHE.get(key)
    if not cached:
        return False
    for c in cached:
        session.cookies.set(c["name"], c["value"], domain=c["domain"])
    return True


def _invalidate_waf_cookies(key: str) -> None:
    with _waf_cache_lock:
        _WAF_COOKIE_CACHE.pop(key, None)


class SSOClient:
    def __init__(self, timeout: int = None, proxy: str = None):
        self.gw = config.SSO_GW
        self.timeout = timeout or config.REQUEST_TIMEOUT
        # 原始代理串（requests 用 + WafSolver.browser_post 的浏览器 context
        # 共用同一个出口 —— 两者必须同源，见 _post 挑战分支）
        self.proxy = proxy
        self.session = requests.Session()
        # 出口代理。目标站点的封禁是 IP 维度，换 IP 靠这里。
        # `proxy` 传具体值时只作用于这个 client（槽位池并发场景必须这样用 ——
        # 改全局 `config.IR_PROXY` 在多个 producer 之间会互相踩）；
        # 传 None 时退回全局 `IR_PROXY`。
        # 注意 `apply_proxy` 会同时关掉 `trust_env` —— 否则环境里的
        # `HTTP_PROXY`（本机是 Clash）会把我们指定的代理**静默盖掉**。
        config.apply_proxy(self.session, proxy)
        self.proxy = proxy
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "lang": "zh-CN",
            "Origin": config.SSO_BASE,
            "User-Agent": config.USER_AGENT,
        })
        # 🔴 撞挑战页时**放弃解题、换出口**（见 _post 的 abandon_on_challenge）。
        #    默认 False —— 筛槽位（screen_slots）要靠挑战页判出口好坏，
        #    不能放弃；只有注册产线（pipeline 拿到槽位租约后）会打开它。
        self.abandon_on_challenge = False

    def _headers(self, referer_path: str = "/register") -> dict:
        h = dict(self.session.headers)
        h["Referer"] = f"{config.SSO_BASE}{referer_path}"
        return h

    @staticmethod
    def _is_waf_challenge(r: requests.Response) -> bool:
        """阿里云 WAF 的拦截页（2026-09-30 起拦写接口）。

        🔴 2026-10-13 修订：不能再靠关键字判。WAF 现在还会下发 **`aliyun_waf_aa`
        变体**（`<!doctype html><meta name="aliyun_waf_aa" content="...">`），
        页内**没有** `acw_sc__v2` / `aliyunwaf` 关键字 —— 旧判据漏检 → `_post`
        把它当正常响应返回 → `r.json()` 抛 `Expecting value`（本机实测）。

        新判据：**注册 API 本该只返回 JSON**。任何 HTTP 200 + `text/html`
        都是 WAF 拦截页，一律交给解题路径处理。
        """
        ct = (r.headers.get("content-type") or "").lower()
        return r.status_code == 200 and "text/html" in ct

    # 阿里云 WAF 初级挑战的标准算法（公开逆向结论，2026-09-30 实测有效）：
    # arg1（服务端 40 位 hex）与固定 mask 逐字节异或，再按位置表重排。
    # 🔴 为什么不再用浏览器算（历史见 waf_bypass.py）：Resin 网关是
    #    **每请求轮换出口**（实测同用户名 4 发 3 个不同 IP），浏览器算出
    #    cookie 的出口与 requests 重放的出口必然不同，实测重放仍被拦。
    #    纯算法在同一个 session 内"首发→算→重放"，连接复用保住出口，
    #    2026-09-30 服务器实测：重放拿到 429 JSON（业务限流）——穿透成功。
    _WAF_MASK = "3000176000856006061501533003690027800375"
    _WAF_POS = [15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23,
                25, 13, 6, 11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17,
                5, 3, 28, 34, 37, 12, 36]

    @classmethod
    def _acw_sc_v2(cls, arg1: str) -> str:
        mask, pos = cls._WAF_MASK, cls._WAF_POS
        xored = []
        for i in range(0, min(len(mask), len(arg1)), 2):
            xored.append(f"{int(mask[i:i + 2], 16) ^ int(arg1[i:i + 2], 16):02x}")
        res = [""] * 40
        for i, p in enumerate(pos):
            if i < len(xored):
                res[p - 1] = xored[i]
        return "".join(res)

    def _solve_waf(self, r: requests.Response, method: str = "js") -> None:
        """解挑战页 → 把 cookie 塞进本 session → 由调用方重放原请求。

        🔴 三种解法必须**轮换使用**，不能固定优先级：
            js       —— 引擎执行页面动态算法（waf_js，理想路径）
            browser  —— 真实浏览器执行（waf_bypass.solve，昨晚 10/10 靠它）
            algo     —— 旧版固定 mask/poslist（只匹配早期挑战，常无效）
        坑在于：**无法本地判断算出的 cookie 是否有效** —— 旧算法永远
        "匹配成功"却给出废 cookie，若固定优先级就永远轮不到浏览器兜底
        （2026-09-30 实测：批量 4/4 全挑战就是这个原因）。所以由调用方
        重放后仍被挑战时，换下一种解法再试。

        🔴 `acw_tc` 不用手动塞：首发响应的 `set-cookie` 已被 requests 自动
           收进 `self.session.cookies`（探针实测漏带它会被服务端**挂起**）。
        """
        if method == "js":
            from .waf_js import solve_acw

            val = solve_acw(r.text)
            if val:
                self.session.cookies.set(
                    "acw_sc__v2", val, domain="sso.openxlab.org.cn", path="/")
                return
            raise RuntimeError("js engine 未解出 cookie")
        if method == "browser":
            from .waf_bypass import WafSolver

            cookies = WafSolver.get().solve(r.text)
            for k, v in cookies.items():
                self.session.cookies.set(
                    k, v, domain="sso.openxlab.org.cn", path="/")
            return
        import re as _re

        m = _re.search(r"arg1='([0-9A-Fa-f]{40})'", r.text)
        if not m:
            raise RuntimeError("挑战页 arg1 形态不匹配，旧算法不可用")
        self.session.cookies.set(
            "acw_sc__v2", self._acw_sc_v2(m.group(1)),
            domain="sso.openxlab.org.cn", path="/")

    def _post(self, path: str, payload: dict, *, referer: str = "/register",
              attempts: int = 8,
              abandon_on_challenge: bool = False) -> requests.Response:
        """带退避重试的 POST。

        🔴 `abandon_on_challenge=True`：遇到挑战页**立刻放弃**（抛
        `WafChallengeError`），不走 js/browser/algo 轮换解题。

        为什么要有这个开关（2026-10-01 实测，用户点名"注册脚本卡死"）：
            WAF_SOLVE_TIMEOUT=90s，browser 兜底单次最多 90s；_post 最多
            4 次尝试 → 一个号最长卡 **6 分钟零输出**，整批看起来像死了。
        但**池化模式下换出口比解题快得多**：池子里本来就有真能注册的
            CLEAR 出口（screen 筛过），ChallENGING 只说明"这个出口现在不行"。
        所以注册产线走 abandon：几秒放弃 → report_failed → 换下一个出口。
        筛槽位（screen_slots）仍走解题路径 —— 它要靠挑战页判出口好坏。

        🔴 为什么必须有：`register/byEmail` 有写操作限流。实测 4 路并发注册时
        3 路立刻拿到 `429 Too Many Requests`（~1.2s 就返回，不是超时）。
        把 429 当注册失败会让批量任务大面积假失败 —— 它只是"慢点再来"。

        ⚠ 2026-09-20 删掉了原先的 `auth: str = None` 形参（连同一个
        `if auth: h["Authorization"] = …` 分支）：它唯一的调用者是已删除的
        `internal_auth()`，删后全仓无调用者传 `auth=`（已 grep 确认）。

        2026-09-30 两次迭代：
          - 挑战页出现时，**主路径改为浏览器内闭环发请求**
            （`WafSolver.browser_post`：真实页面过 WAF 后同上下文 fetch，
            cookie/出口/会话信誉三者一致）。纯算法 cookie（`_solve_waf`）
            能过 WAF 但会话信誉低，网关对低分会话 429 —— 只当兜底；
          - 走 rotating 代理（Resin）时部分出口是黑洞（POST 挂到 read
            timeout），Timeout/ConnectionError 也纳入退避重试。

        Returns:
            requests.Response，或挑战路径下的兼容对象（`.status_code` /
            `.text` / `.json()` 同形状）—— 调用方无感。
        """
        h = self._headers(referer)
        url = f"{self.gw}{path}"
        last = None
        key = _waf_key(self.proxy)
        # 挑战解法轮换：算出的 cookie 无法本地验证，只能靠重放结果判断 ——
        # 仍被挑战就换下一种（旧算法 / js 引擎 / 真实浏览器）
        #
        # 挑战解法轮换：算出的 cookie 无法本地验证，只能靠重放结果判断 ——
        # 仍被挑战就换下一种。
        #
        # 🔴 2026-10-13 实测（服务器）：`js` 引擎解法对当前挑战页**恒返回空串**
        #    （stub 环境跑不出 cookie），`algo`（旧固定 mask）只在少数挑战上有效，
        #    而 **`browser`（Camoufox 假页面执行挑战 JS）稳定成功**（实测
        #    `solve browser OK cookie=True`，且重放能到业务层）。所以让每次重试都
        #    重跑 browser（挑战页每次下发新 arg1，必须重算），`algo` 仅作最后兜底。
        solvers = ["browser", "browser", "algo"]
        used = 0
        for i in range(attempts):
            try:
                r = self.session.post(url, headers=h, json=payload,
                                      timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as ex:
                # 🔴 走 rotating 代理（Resin）时部分出口节点是黑洞：
                #    POST 挂到 read timeout。这**不是**注册失败，是链路抖动
                #    —— requests 的连接池会换连接，重试经常就好。
                last = ex
                if i == attempts - 1:
                    break
                time.sleep(min(1.5 * (2 ** i), 8.0) + random.uniform(0, 1))
                continue
            if self._is_waf_challenge(r):
                # 🔴 池化产线：放弃解题，换出口（见 abandon_on_challenge 的说明）
                if abandon_on_challenge:
                    _invalidate_waf_cookies(key)  # P0-2: 同出口复用命中仍被拦→缓存失效
                    raise WafChallengeError(
                        f"WAF 挑战页，放弃本出口（{r.status_code}）")
                last = r
                import sys as _sys
                # 🔴 2026-10-13：`aliyun_waf_aa` = 阿里云**滑块验证页**。
                #    requests 侧三种解法（js 引擎 / 假页面 browser / 旧 algo）
                #    对它**全都无解** —— 假页面跑不出滑块 cookie（实测只得到 []）。
                #    唯一能过的是**真实浏览器闭环**：`browser_post` 内置
                #    `solve_aliyun` 拖滑块过验证，再在同一上下文 fetch。
                #    所以命中滑块页 → 把本次请求整体交给浏览器路径。
                if "aliyun_waf_aa" in r.text:
                    try:
                        from .waf_bypass import WafSolver
                        res = WafSolver.get().browser_post(
                            f"{self.gw}{path}", payload, proxy=self.proxy)
                        wrapped = _WrapResponse(res)
                        wrapped.json()          # 能解析成 JSON = 已到业务层
                        _cache_waf_cookies(key, self.session)
                        print(f"[WAFDBG] aa→browser_post OK status={wrapped.status_code}",
                              file=_sys.stderr, flush=True)
                        # 🔴 429 = 注册写接口限流（WAF 已穿过）。这类限流是
                        #    **按出口 IP** 记的 —— 同一出口连着重试只会被继续拒，
                        #    换出口才拿得到业务响应。所以这里**不再原地重试**，
                        #    把 429 交回 `register()` 分类 → pipeline 换槽位。
                        return wrapped
                    except Exception as _e:
                        print(f"[WAFDBG] aa→browser_post FAIL {type(_e).__name__}: {str(_e)[:140]}",
                              file=_sys.stderr, flush=True)
                    # 滑块页：假页面解法无解，别再空烧 90s×N
                    break
                if used >= len(solvers):
                    break
                method = solvers[used]
                used += 1
                print(f"[WAFDBG] challenge i={i} method={method} len={len(r.text)}", file=_sys.stderr, flush=True)
                try:
                    self._solve_waf(r, method=method)
                    print(f"[WAFDBG] solve {method} OK cookie={bool(self.session.cookies.get('acw_sc__v2'))}", file=_sys.stderr, flush=True)
                except Exception as _e:
                    print(f"[WAFDBG] solve {method} FAIL {type(_e).__name__}: {str(_e)[:120]}", file=_sys.stderr, flush=True)
                    pass       # 解法本身失败 → 换下一种,不中断重试链
                else:
                    _cache_waf_cookies(key, self.session)  # P0-2: 解出后缓存供复用
                continue
            # 🔴 405 = WAF 判这个出口高风险，**不要重试**（重试只会烧同一出口的信誉）
            if r.status_code == 405:
                last = r
                break
            # 🔴 2026-10-13 实测：429 是 `register/byEmail` 的**按出口 IP** 记的
            #    写限流 —— 同一出口连着重试只会被继续拒（日志里连续两次 429），
            #    而换出口立刻能拿到业务响应（A0101/A0216）。所以在**同一出口**
            #    上只给一次机会（区分瞬时限流），然后把 429 交回调用方 →
            #    `register()` 抛 `RateLimitedError` → pipeline 换槽位重试。
            #    之前这里会退避 8 次（最坏 ~5 分钟），既没用又在继续烧出口。
            if r.status_code == 429:
                if i >= 1:
                    return r
                time.sleep(8.0 + random.uniform(0, 3.0))
                continue
            if r.status_code >= 500:
                last = r
                if i == attempts - 1:
                    break
                # 优先用服务端给的 Retry-After，没有就指数退避。
                ra = r.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra else 1.5 * (2 ** i)
                except (TypeError, ValueError):
                    delay = 1.5 * (2 ** i)
                delay = min(delay, 45.0) + random.uniform(0, 2.0)
                time.sleep(delay)
                continue
            _cache_waf_cookies(key, self.session)  # P0-2: 命中后顺手收下 WAF cookie
            import sys as _sys
            if "text/html" in (r.headers.get("content-type") or "").lower():
                try:
                    import os as _os
                    _p = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), "logs", "waf_block.html")
                    open(_p, "w", encoding="utf-8").write(r.text)
                except Exception:
                    pass
            print(f"[WAFDBG] pass i={i} status={r.status_code} ct={(r.headers.get('content-type') or '')[:30]} len={len(r.text)}", file=_sys.stderr, flush=True)
            return r
        if isinstance(last, requests.RequestException):
            raise last
        last.raise_for_status()
        return last

    # ── 可用性校验 ────────────────────────────────────────────
    def check_username(self, username: str) -> bool:
        """True 表示用户名可用。"""
        r = self._post("/personal/username/check", {"username": username})
        return r.json().get("data") is True

    # ── 注册 ──────────────────────────────────────────────────
    def prime_session(self) -> None:
        """GET 一次注册页，种下 `acw_tc` 会话链。

        🔴 2026-09-30 终局实测：跳过这一步时，裸 POST 的挑战页重放即便
        算法 cookie 正确也会被持续挑战（会话链断裂）；先 GET 种会话后，
        首个 POST **直达业务层**（无挑战）。有头浏览器能过的原因同此 ——
        它天然带了完整的 GET → JS → cookie 链。协议路径补上即等价。
        """
        key = _waf_key(self.proxy)
        _seed_waf_cookies(key, self.session)  # P0-2: 同出口复用已解出的 WAF cookie
        try:
            self.session.get(f"{config.SSO_BASE}/register",
                             headers=self._headers("/register"),
                             timeout=self.timeout)
        except requests.RequestException:
            # 种会话失败不致命：后面的 _post 遇到挑战页仍有算法兜底
            pass
        finally:
            # 收下本次种下的 / 解出的 WAF cookie，供同出口后续客户端跳过解题
            _cache_waf_cookies(key, self.session)

    def register(self, username: str, email: str, password: str) -> RegisterResult:
        payload = {
            "username": username,
            "email": email,
            "password": encrypt_password(email, password),
            "source": config.SOURCE,
            "clientId": config.CLIENT_ID,
        }
        r = None
        # 🔴 2026-10-13：注册默认走**真实浏览器主路径**。
        #    requests 路径注册一次要发 3+ 次写请求（裸 POST → 挑战 → 解题 →
        #    重放 → 再遇滑块 → 转浏览器），同出口短时间多次写 ⇒ 触发按 IP
        #    记的 429。浏览器一次过 WAF，只发一次写请求（见 config 的说明）。
        if config.REGISTER_VIA_BROWSER:
            try:
                from .waf_bypass import WafSolver
                # 🔴 2026-10-19 换主路径：真实 **UI 表单**注册。站方按
                #    「非交互式 fetch」扣激活信（探针矩阵：UI→6s 收信，
                #    fetch 直发→0 收，同出口同域）。UI 失败仍退回 fetch。
                res = WafSolver.get().browser_register_ui(
                    username, email, password, proxy=self.proxy)
                _r = _WrapResponse(res)
                _r.json()          # 必须是业务 JSON 才算数（挑战页会在这里炸）
                r = _r
                import sys as _sys
                print(f"[WAFDBG] register via UI OK status={_r.status_code}",
                      file=_sys.stderr, flush=True)
            except Exception as _e:
                import sys as _sys
                print(f"[WAFDBG] register via UI FAIL {type(_e).__name__}: {str(_e)[:160]}",
                      file=_sys.stderr, flush=True)
                r = None            # UI 路径没拿下 → 退回 fetch 路径
            if r is None:
                try:
                    from .waf_bypass import WafSolver
                    # 🔴 2026-10-20 C0204 修复：密码密文带秒级时间戳，UI 路径
                    #    超时后走 fetch 回退时密文已 150s+ ⇒「加密参数过期」。
                    #    每条路径发出前**重新加密**，时间戳永远新鲜。
                    payload["password"] = encrypt_password(email, password)
                    res = WafSolver.get().browser_post(
                        f"{self.gw}/register/byEmail", payload, proxy=self.proxy)
                    _r = _WrapResponse(res)
                    _r.json()
                    r = _r
                    import sys as _sys
                    print(f"[WAFDBG] register via browser OK status={_r.status_code}",
                          file=_sys.stderr, flush=True)
                except Exception as _e:
                    import sys as _sys
                    print(f"[WAFDBG] register via browser FAIL {type(_e).__name__}: {str(_e)[:160]}",
                          file=_sys.stderr, flush=True)
                    r = None            # 浏览器路径没拿下 → 退回 requests 路径
        if r is None:
            # 🔴 2026-10-20 C0204 修复：requests 回退前同样重新加密（此时
            #    距函数入口可能已 300s+，旧密文必过期）
            payload["password"] = encrypt_password(email, password)
            r = self._post("/register/byEmail", payload,
                           abandon_on_challenge=self.abandon_on_challenge)
        body = r.json()
        # 🔴 限流必须先于 `success` 判定：429 的响应体是
        #    `{"error": "Too many request"}` —— 既没有 `success` 也没有
        #    `msgCode`，直接查 success 会把它当成普通业务失败（ERR_REJECTED）。
        if r.status_code == 429 or "too many request" in (
                f"{body.get('error') or ''} {body.get('msg') or ''}").lower():
            raise RateLimitedError(
                f"register/byEmail 限流（{r.status_code} "
                f"{body.get('error') or body.get('msg') or ''}）")
        data = body.get("data") or {}
        # 🔴 注册成功的响应体是
        #    `{"traceId":"...","msgCode":"10000","msg":"ok","data":{"ssoUid":...}}`
        #    —— **没有 `success` 字段**！只判 `success is True` 会把**注册成功的
        #    账号当成失败**（浏览器路径实测拿到 msgCode 10000 + ssoUid，却会被
        #    判成 ERR_REJECTED → `register failed: 10000 ok`，整批 0 成功）。
        #    所以 `msgCode == "10000"` 也算成功（实测 `msg` 同时是 "ok"）。
        _ok = (body.get("success") is True
               or str(body.get("msgCode") or "") == "10000")
        return RegisterResult(
            ok=_ok,
            sso_uid=str(data.get("ssoUid", "")),
            email=data.get("email", ""),
            username=data.get("username", ""),
            msg_code=body.get("msgCode", ""),
            msg=body.get("msg", ""),
        )

    # ── 激活 ──────────────────────────────────────────────────
    def activate(self, token: str, sign: str) -> bool:
        r = self._post("/register/active", {"token": token, "sign": sign},
                       referer="/active")
        return r.json().get("success") is True

    def activate_from_url(self, url: str) -> bool:
        """从激活链接中解析 token/sign 并激活。"""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        token = (qs.get("token") or [""])[0]
        sign = (qs.get("sign") or [""])[0]
        if not token or not sign:
            raise ValueError(f"activation url missing token/sign: {url}")
        return self.activate(token, sign)
