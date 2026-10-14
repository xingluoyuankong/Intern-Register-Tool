"""浏览器会话与重试循环。

- `_launch_kwargs()` 是阶段 A 引入的**注入面** —— `chrome_args` 为 `None` 时
  用 `constants.CHROME_ARGS`，否则用传入值。这让探针不必再改写模块全局。
- `_retry_loop()` 消掉了 `login()` 与 `BrowserSession.login()` 约 20 行重复。
"""

import os
import random
import time

from .. import config
from .attempt import _run_attempt
from .constants import CHROME_ARGS
from .launch import current_engine, launch_browser
from .state import LoginResult


# ────────────────────────────────────────────────────────────────
# 启动参数与重试循环（两个入口共用）
# ────────────────────────────────────────────────────────────────
def _launch_kwargs(headless: bool, chrome_args=None) -> dict:
    """组装 `chromium.launch()` 的参数。

    🔴 `chrome_args` 必须是**显式参数**，不能只靠调用方去改模块级 `CHROME_ARGS`。

    探针 `tools/probes/probe_headless.py` 原先就是这么干的：
    `bl.CHROME_ARGS = BASE_ARGS + extra_args`。那是"改模块全局命名空间"的写法，
    依赖"模块属性赋值 == 改该模块的全局变量"这条 Python 语义。它现在能工作，
    但只要本模块被**拆包或加兼容壳**，这种注入就会**静默失效** ——
    改的是壳的属性，真源没变，而且不报错，探针会照常跑完并给出失真的结论。

    改成参数后，注入面不再依赖模块内部布局。`None` → 用模块级 `CHROME_ARGS`，
    所以旧调用方零改动。

    🔴 2026-09-30：**无头模式改用 playwright 自带 chromium**（headless shell）。
       实测本机的系统 Chrome（用户级安装）被 playwright 1.63 驱动时，
       `browser.close()` 会把 driver 连接弄断、进程挂死 5 分钟 ——
       整个批量跑批因此卡住（run.py 日志停在 "browser ready" 后无输出）。
       headless shell 无此问题。有头模式（--headful）仍用 `CHROME_PATH`。

    🔴 2026-09-30：**浏览器默认跟随环境代理**。实测本机 requests 走
       `http_proxy=127.0.0.1:31777` 一切正常，而 Chrome 直连
       `sso.openxlab.org.cn/login` 直接 `ERR_CONNECTION_CLOSED` ——
       登录阶段因此全灭（注册是 requests 走的，所以不受影响）。
       Chromium 的 `proxy` launch 参数让它跟 requests 同路径。
       `IR_BROWSER_PROXY=0` 可关（要直连调试时用）。
    """
    if headless:
        from ..waf_bypass import find_headless_browser
        exe = find_headless_browser()
    else:
        exe = config.CHROME_PATH
    args = list(CHROME_ARGS) if chrome_args is None else list(chrome_args)
    kw = dict(
        executable_path=exe,
        headless=headless,
        args=args,
    )
    if os.getenv("IR_BROWSER_PROXY", "0").strip().lower() not in ("0", "false", "no"):
        proxy = (os.getenv("IR_BROWSER_PROXY", "").strip()
                 or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
                 or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
        if proxy:
            # 2026-10-02：g.alicdn.com（阿里验证码 SDK 源）**直连不可达**，
            # 必须走 Clash 代理才返回 200；而 sso 又必须绕开 Clash（见下方
            # 直连分支注释：Clash 把 sso 路由到国外节点 → ERR_CONNECTION_CLOSED）。
            # 所以代理模式要把 sso 放进 bypass 走真实直连，其余（含 g.alicdn.com）
            # 走 Clash，验证码才能加载出来。
            # sso 登录页是 JS 渲染的，它的 JS/CSS 资源从 openxlab/intern-ai
            # 的子域加载；这些子域必须走真实直连（此前纯直连 = --no-proxy-server
            # 时一切正常）。只有 g.alicdn.com 这类外部 CDN 必须走 Clash 代理
            # （直连不可达）。所以把整个 openxlab/intern-ai 域 bypass 直连，
            # 其余（含验证码 SDK 源）走代理。
            kw["proxy"] = {"server": proxy, "bypass": ".openxlab.org.cn,.intern-ai.org.cn"}
    else:
        # 🔴 2026-10-02 实测（本机 Clash 系统代理=7897）：
        #    **不加 launch proxy 时，Chrome 仍会走 Windows 系统代理** ——
        #    本机系统代理指向 Clash(7897)，Clash 把 `sso.openxlab.org.cn`
        #    路由到**国外节点**（出口实测 54.168.101.58 日本 / 18.138.188.208 新加坡），
        #    国外节点到国内 sso 的 TLS 握手直接失败 → `ERR_CONNECTION_CLOSED`
        #    / 页面永远加载不出（用户看到的"浏览器卡死/自动挂掉"）。
        #    而 curl `--noproxy` 直连 sso = 200 OK 0.40s，证明**真实国内直连是通的**。
        #    ⇒ 必须显式 `--no-proxy-server` 让 Chrome 绕开系统代理，才真正直连。
        #    （`IR_BROWSER_PROXY=1` 时仍走代理，供确有需要时用。）
        args.append("--no-proxy-server")
    return kw


def _retry_loop(run_once, *, attempts: int, cooldown: float, verbose: bool,
                retry_hint: str) -> LoginResult:
    """失败重试的公共骨架。

    `run_once(tag) -> LoginResult` 由调用方提供，它自己负责浏览器的创建或复用 ——
    这正是两个入口**唯一**的差别：

      - `login()`                 每次尝试新建浏览器（`with sync_playwright()`）
      - `BrowserSession.login()`  复用同一个浏览器，每次只换 context

    原实现把这段循环抄了两遍（约 20 行 × 2），差异只有末句文案，即 `retry_hint`。

    ⚠ 冷却用 `time.sleep`：此刻处于两次尝试之间，没有待泵送的页面网络事件
      （`_run_attempt` 已经返回、context 已关闭或被新 context 取代）。
      这是**唯一**可以安全 sleep 的位置 —— `_run_attempt` 内部一律用
      `page.wait_for_timeout`（见文件头第 3 条）。
    """
    last = LoginResult(ok=False, reason="not attempted")
    for i in range(max(1, attempts)):
        tag = f"_a{i + 1}"
        if verbose and attempts > 1:
            print(f"    [login] === 尝试 {i + 1}/{attempts} ===", flush=True)
        res = run_once(tag)
        res.attempts_used = i + 1
        if res.ok:
            return res
        last = res
        if i < attempts - 1:
            wait = cooldown * (i + 1) + random.uniform(0, 5)
            if verbose:
                print(f"    [login] 第 {i + 1} 次失败（{res.reason}），"
                      f"冷却 {wait:.0f}s 后{retry_hint}", flush=True)
            time.sleep(wait)
    return last


# ────────────────────────────────────────────────────────────────
# 浏览器复用会话
# ────────────────────────────────────────────────────────────────
class BrowserSession:
    """复用一个浏览器进程，每个账号开一个全新 context。

    Chrome 冷启动约 1.5~2s，批量场景下这笔开销乘以账号数。
    复用浏览器只换 context（cookie / localStorage / 缓存全新建），
    会话隔离性与新开浏览器等价 —— 风控看到的是新会话，不是新进程。

    用法：
        with BrowserSession(headless=True) as sess:
            for acct in accounts:
                res = sess.login(acct.email, acct.password, verbose=True)

    ⚠ Playwright 同步 API 不能跨线程共享。多线程并发时，
      **每个线程各自建一个 BrowserSession**。

    `chrome_args`：自定义 Chrome 启动参数，`None` → 模块级 `CHROME_ARGS`。
    理由见 `_launch_kwargs()` —— **探针要注入启动参数请用它，别改全局**。
    """

    def __init__(self, headless: bool = True, chrome_args=None, proxy: str = None):
        self.headless = headless
        self.chrome_args = chrome_args
        # 🔴 Camoufox 的代理是**启动级**：一个实例固定一个出口。
        self.proxy = proxy
        self._cm = None            # Camoufox 上下文管理器
        self._pw = None
        self._browser = None
        self.launch_ms = 0

    def __enter__(self):
        t0 = time.time()
        self._cm = launch_browser(self.proxy, headless=self.headless)
        self._browser = self._cm.__enter__()
        self.launch_ms = round((time.time() - t0) * 1000)
        return self

    def __exit__(self, *exc):
        """Camoufox 的 `__exit__` 会 close 浏览器并拆掉 playwright 会话。

        🔴 必须走它的 `__exit__`（不是 `kill_driver_tree`）：会话不拆的话，
        同一线程后续任何 `Camoufox()` / `sync_playwright().start()` 都会报
        "Sync API inside the asyncio loop"。
        """
        cm, self._cm, self._browser = self._cm, None, None
        if cm is not None:
            try:
                cm.__exit__(*exc)
            except Exception:
                pass
        return False

    def login(self, account: str, password: str, *, timeout: int = 150,
              attempts: int = 3, cooldown: float = 15.0,
              screenshot_prefix: str = None, verbose: bool = False,
              proxy: str = None) -> LoginResult:
        """登录，失败换新 context 重试。

        proxy: 该账号走哪个出口。🔴 Camoufox 下**代理是启动级** —— 传了与本
            会话不同的出口时，会绕过复用、每次尝试**新建浏览器**
            （换出口必须换实例，Firefox 不支持 per-context 换代理）。
        """
        # 🔴 换出口必须换实例（Camoufox 的代理是启动级），但**不能并行开第二个
        #    浏览器**：playwright 的 event loop 是**线程级**的 —— `start()` 后
        #    `run_until_complete` 把 `asyncio._running_loop` 挂在该线程上，且不随
        #    greenlet 切回而清除。同线程第二次 `start()` 时
        #    `PlaywrightContextManager.__enter__` 会看到 `loop.is_running()` 为真，
        #    直接抛 "Sync API inside the asyncio loop"。
        #    正解：先 `__exit__`（拆会话、清掉 running loop），再 `__enter__` 重建。
        if (proxy and proxy != self.proxy
                and current_engine() == "camoufox"):
            self.proxy = proxy
            self.__exit__(None, None, None)
            self.__enter__()

        def run_once(tag):
            return _run_attempt(self._browser, account=account,
                                password=password,
                                headless=self.headless, timeout=timeout,
                                screenshot_prefix=screenshot_prefix,
                                verbose=verbose, tag=tag,
                                # Camoufox：代理已在启动级注入，context 级不设
                                proxy=(None if current_engine() == "camoufox"
                                       else proxy),
                                fingerprint_browser=(current_engine()
                                                     == "camoufox"))

        return _retry_loop(run_once, attempts=attempts, cooldown=cooldown,
                           verbose=verbose, retry_hint="重试")
