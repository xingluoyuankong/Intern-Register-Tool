"""阿里云 WAF（acw_sc__v2 JS 挑战）破解器 —— 浏览器内闭环发注册请求。

2026-09-30 实测的三个阶段（都撞过，结论在这）：

  ① `requests` 直发 → 挑战页 HTML（HTTP 200 + `arg1` 混淆 JS），不是 JSON。
  ② 「requests 首发zal → 浏览器算 cookie → requests 重放」：要求三步同出口。
     Resin 网关是**每请求轮换出口**（同用户名 4 发 3 个不同 IP 实测），
     三步跨三个出口必然失败；昨晚 batch3 成功纯属当时出口稳定。
  ③ 纯 python 算法（arg1 异或+重排，见 sso.py `_acw_sc_v2`）不跨出口了，
     重放能拿到响应 —— 但会话信誉低，网关对低分会话 429 限流。

  终极解（本文件现状）：**让真实浏览器发注册请求** ——
  `page.goto(sso/register)` 时 WAF JS 在真实环境自动执行（高信誉），
  然后同一页面上下文内 `fetch()` 注册 POST，cookie/出口/信誉天然一致。
  返回 `{"status": int, "text": str}`，由调用方包装。

🔴 实测坑（只存结论）：
  - `browser.close()` / `sync_playwright().stop()` 在本机会**挂死** →
    本模块的浏览器从不 close，退出用 `atexit` + `kill_driver_tree` 兜底；
  - headless shell 在三平台的可执行文件路径见 `find_headless_browser`；
  - playwright 同步 API 绑定创建线程 → 全部 playwright 操作都在
    专用线程（`_loop`）里，对外经队列提交，任意线程可调。
"""

import atexit
import glob
import os
import queue
import re
import subprocess
import threading
import time

from . import config


# ── 浏览器可执行文件发现 ─────────────────────────────────────────
def find_headless_browser() -> str:
    """headless 用 playwright 自带 chromium（系统 Chrome 的 close 会挂死）。

    顺序：`IR_WAF_BROWSER_PATH` → playwright 缓存里**最新**的
    chromium_headless_shell → playwright 配套 chromium **完整版**
    → 回退 `config.CHROME_PATH`（**下策**，见文末警告）。
    三平台缓存布局（playwright ≥1.49，headless shell 独立成包后
    各平台目录名统一为 chrome-headless-shell-<os><arch>）：
      Windows  ~/AppData/Local/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe
      Linux    ~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell
      macOS    ~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell
    """
    env = os.getenv("IR_WAF_BROWSER_PATH", "").strip()
    if env and os.path.isfile(env):
        return env
    home = os.path.expanduser("~")
    roots: list[str] = []
    # 🔴 `PLAYWRIGHT_BROWSERS_PATH` 常被指到**非 home** 目录（本机是
    #    `F:\Playwright\ms-playwright`）。原来只搜 home 下三个默认位置，
    #    于是这个盘里的浏览器**一个都看不见** → 静默回退系统 Chrome → 挂死。
    pb = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if pb:
        roots.append(pb)
    roots += [
        os.path.join(home, "AppData", "Local", "ms-playwright"),
        os.path.join(home, ".cache", "ms-playwright"),
        os.path.join(home, "Library", "Caches", "ms-playwright"),
    ]
    shell: list[str] = []    # 纯 headless shell（最优）
    full: list[str] = []     # playwright 配套**完整版** chromium（次优）
    for root in roots:
        shell += glob.glob(os.path.join(
            root, "chromium_headless_shell-*", "*", "chrome-headless-shell.exe"))
        shell += glob.glob(os.path.join(
            root, "chromium_headless_shell-*", "chrome-headless-shell-*",
            "chrome-headless-shell"))
        shell += glob.glob(os.path.join(
            root, "chromium_headless_shell-*", "chrome-headless-shell-*",
            "chrome-headless-shell.exe"))
        shell += glob.glob(os.path.join(
            root, "chromium_headless_shell-*", "chrome-linux*", "headless_shell"))
        shell += glob.glob(os.path.join(
            root, "chromium_headless_shell-*", "chrome-mac*", "headless_shell"))
        # 🔴 第二优先级：只装了 `chromium-<build>`（完整版，没单独下
        #    headless shell 包）时的兜底。它是 **playwright 配套的**，
        #    launch/close 都正常；真正会挂死的是最后那个**系统 Chrome**。
        full += glob.glob(os.path.join(
            root, "chromium-*", "chrome-win64", "chrome.exe"))
        full += glob.glob(os.path.join(
            root, "chromium-*", "chrome-win", "chrome.exe"))
        full += glob.glob(os.path.join(
            root, "chromium-*", "chrome-linux*", "chrome"))
        full += glob.glob(os.path.join(
            root, "chromium-*", "chrome-mac*", "Chromium.app",
            "Contents", "MacOS", "Chromium"))
    if shell:
        # 版本号排序取最新（目录名 chromium_headless_shell-<build>）
        return sorted(shell)[-1]
    if full:
        return sorted(full)[-1]
    # ⚠ 到这一步只剩系统 Chrome —— 它被 playwright 驱动时 close() 挂死
    #   （2026-09-30 实测，曾让 run.py 卡 5 分钟）。能不走到这里就别走。
    return config.CHROME_PATH


def kill_driver_tree(pw) -> None:
    """杀掉 playwright driver（node）进程树 —— 连着里面的浏览器一起。

    为什么不用 `browser.close()`：本机实测它**挂死**（不是抛异常，是永远不
    返回）。playwright 没有官方的"浏览器进程 PID"接口，而 driver（node）是
    python 的子进程、chrome 是 node 的子进程，所以杀 driver 的进程树 =
    全部收掉。Windows 用 `taskkill /F /T`；POSIX 递归读
    `/proc/<pid>/task/<tid>/children` 自顶向下 SIGKILL。
    """
    if pw is None:
        return
    try:
        proc = pw._connection._transport._proc       # noqa: SLF001 内部结构
        pid = getattr(proc, "pid", None)
    except Exception:
        pid = None
    if not pid:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=15, check=False)
        except Exception:
            pass
        return
    # POSIX：/proc 遍历，先杀叶子再杀根
    try:
        to_kill: list[int] = []

        def walk(p: int):
            to_kill.append(p)
            try:
                with open(f"/proc/{p}/task/{p}/children") as f:
                    for c in f.read().split():
                        walk(int(c))
            except OSError:
                pass

        walk(int(pid))
        for p in reversed(to_kill):
            try:
                os.kill(p, 9)
            except OSError:
                pass
    except Exception:
        pass


# ── 破解器 ───────────────────────────────────────────────────────
class WafSolver:
    """单例。任意线程可调；playwright 生命周期全部在专用线程里。"""

    _lock = threading.Lock()
    _instance = None

    @classmethod
    def get(cls) -> "WafSolver":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._req_q: queue.Queue = queue.Queue()
        self._resp_q: queue.Queue = queue.Queue()
        self._started = False
        self._start_lock = threading.Lock()
        self._seq = 0
        self._seq_lock = threading.Lock()

    def _next_token(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    # ── 对外 ─────────────────────────────────────────────────
    def solve(self, challenge_html: str) -> dict[str, str]:
        """[兜底] 算出挑战页要求的 cookie（阻塞，最长 `WAF_SOLVE_TIMEOUT` 秒）。"""
        self._ensure_started()
        tok = self._next_token()
        self._req_q.put((tok, ("solve", challenge_html)))
        return self._wait(tok)

    def browser_post(self, path: str, payload: dict,
                     proxy: str = None) -> dict:
        """[主路径] 真实浏览器过 WAF 后，在同一上下文 fetch 注册接口。

        `proxy`：Resin 槽位串（`http://user:@host:port`）—— 每次调用
        **新建 context**（cookie 隔离 + 指定出口），用完即关。与 requests
        的 SSOClient 传同一串，保证两条链路同出口。
        返回 `{"status": int, "text": str}`。阻塞至多 `WAF_SOLVE_TIMEOUT` 秒。
        """
        self._ensure_started()
        tok = self._next_token()
        self._req_q.put((tok, ("post", (path, payload, proxy))))
        return self._wait(tok)

    def shutdown(self):
        """杀 driver 进程树（run.py 收尾时调用；atexit 也会兜一次）。"""
        self._req_q.put(None)

    def _wait(self, tok: int):
        """取回 **自己那次**请求的响应。

        🔴 2026-10-13：原来不带 token，`_wait()` 超时返回后，求解线程**仍在跑**
        （浏览器启动/ goto 常常超过 90s），它完成后把响应塞回队列 —— 于是
        **下一次调用会领走上一次的迟到响应**：表现为 JSONDecodeError（空体）、
        "Target page... closed"、"NS_ERROR_NET_TIMEOUT" 这些**张冠李戴**的错。
        现在按 token 配对，不匹配的直接丢弃。
        """
        deadline = time.time() + config.WAF_SOLVE_TIMEOUT
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise RuntimeError(
                    f"WAF 求解超时（>{config.WAF_SOLVE_TIMEOUT}s）")
            try:
                rtok, kind, val = self._resp_q.get(timeout=left)
            except queue.Empty:
                raise RuntimeError(
                    f"WAF 求解超时（>{config.WAF_SOLVE_TIMEOUT}s）")
            if rtok != tok:
                continue        # 上一次超时留下的迟到响应 → 丢弃
            if kind == "err":
                raise RuntimeError(f"WAF 求解失败: {val}")
            return val

    # ── 专用线程 ─────────────────────────────────────────────
    def _ensure_started(self):
        with self._start_lock:
            if self._started:
                return
            t = threading.Thread(target=self._loop, daemon=True,
                                 name="waf-solver")
            t.start()
            self._started = True

    def _loop(self):
        """Camoufox 生命周期都在这个线程（同步 API 的线程绑定约束）。

        🔴 这个**单例浏览器只服务 `_do_solve`** —— 它在假页面里执行挑战 JS，
        **不联网**，所以不需要代理。`_do_post` 要带槽位出口，而 Camoufox 的
        代理是**启动级**（Firefox 不支持 per-context 换出口），所以那边每次
        调用都**新建浏览器实例**。
        """
        # 延迟导入：waf_bypass 在 src/ 下，模块级 import browser 会成环。
        from .browser.launch import launch_browser

        def _start(proxy=None, headless: bool = True):
            """新建浏览器，返回 `(上下文管理器, browser)`。

            🔴 同一线程**同时只能存在一个** Playwright 会话：常驻单例 + 再新建
            会直接抛 "Sync API inside the asyncio loop"（注册就是这么挂的）。
            所以 `_do_solve` / `_do_post` 都走这里，**用完立刻 __exit__**。
            """
            cm = launch_browser(proxy, headless=headless)
            try:
                return cm, cm.__enter__()
            except Exception:
                try:
                    cm.__exit__(None, None, None)
                except Exception:
                    pass
                raise

        def _do_solve(challenge_html: str) -> dict[str, str]:
            """[兜底] 假页面执行挑战 JS，读 cookie（独立浏览器，用完即关）。"""
            cm, browser = _start(None, True)
            try:
                return _solve_with(browser, challenge_html)
            finally:
                try:
                    cm.__exit__(None, None, None)
                except Exception:
                    pass

        def _solve_with(browser, challenge_html: str) -> dict[str, str]:
            """在给定 browser 上跑挑战 JS（不联网，所以不需要代理）。"""
            ctx = browser.new_context()
            page = ctx.new_page()
            try:
                html = challenge_html.replace(
                    "document.location.reload()", "void 0;")
                page.route(f"{config.SSO_BASE}/__waf_probe", lambda route: (
                    route.fulfill(status=200,
                                  content_type="text/html; charset=utf-8",
                                  body=html)))
                page.goto(f"{config.SSO_BASE}/__waf_probe",
                          wait_until="load", timeout=20000)
                page.wait_for_timeout(1500)     # 给混淆 JS 一点执行时间
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                if not cookies.get("acw_sc__v2"):
                    raise RuntimeError(
                        f"cookie 没算出来，只得到 {list(cookies)}")
                return cookies
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass

        def _do_post(path: str, payload: dict, proxy: str | None) -> dict:
            """[主路径] 新建指纹浏览器（带槽位代理）→ 真实过 WAF → 同源 fetch。

            🔴 Camoufox 的代理是**启动级**，Firefox 不支持 per-context 换出口
            —— 所以每次调用都新建一个浏览器实例（用完即关，指纹也是全新的）。
            """
            # 指纹浏览器（Camoufox）：代理是**启动级**的，context 不再设。
            cm, br = _start(proxy, False)
            ctx = br.new_context()
            page = ctx.new_page()
            # 🔴 rotating 网关下页面完整资源加载能拖过 90s（图片/字体/统计
            #    脚本各自都要过一次轮换隧道）。WAF 的挑战 JS 是**主文档内联
            #    的**，穿透只需要：主文档 + 脚本 + 我们自己的 fetch。
            #    其余资源全部 abort —— 首页秒开。
            # 🔴 滑块验证要拿到**拼图/背景图**才能算缺口，所以渲染 `aa` 页时
            #    临时放开 image（平时 abort 是为了让主文档秒开 —— rotating
            #    网关下图片/字体太慢，实测能拖过 90s）。
            _allow = {"image": False}

            def _trim(route):
                rt = route.request.resource_type
                if rt in ("document", "script", "xhr", "fetch"):
                    route.continue_()
                elif _allow["image"] and rt == "image":
                    route.continue_()
                else:
                    route.abort()

            page.route("**/*", _trim)
            try:
                # 🔴 2026-10-13 实测：rotating 网关下 30s 常常不够（槽位体检 10 个
                #    里 3 个死在 `Page.goto: Timeout 30000ms`）。放宽到 60s，
                #    并对**瞬时的 goto 失败**重试一次 —— 浏览器路径是目前唯一
                #    能过 `aliyun_waf_aa` 滑块的路径，它挂了就只能退回 requests
                #    多写几次 → 触发按 IP 记的 429。
                for _try in range(2):
                    try:
                        page.goto(f"{config.SSO_BASE}/register",
                                  wait_until="domcontentloaded", timeout=60000)
                        break
                    except Exception:
                        if _try == 1:
                            raise
                        page.wait_for_timeout(1500)
                # 挑战页会自动 reload；cookie 出现 = 已穿透
                deadline = time.time() + 25
                while time.time() < deadline:
                    if "acw_sc__v2" in {c["name"] for c in ctx.cookies()}:
                        break
                    page.wait_for_timeout(500)
                page.wait_for_timeout(800)      # 页面 JS 收尾
                js = """async (args) => {
                    const r = await fetch(args.path, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        credentials: 'include',
                        body: JSON.stringify(args.payload),
                    });
                    return {status: r.status, text: await r.text()};
                }"""
                # 🔴 path 归一：调用方可能传 gw 相对路径（`/register/byEmail`）
                #    或完整 URL。fetch 需要能落到 `SSO_GW` 前缀上，否则会打到
                #    `/register/byEmail`（少一层 gw/uaa-be/api/v1）拿 404/挑战。
                if not str(path).startswith("http"):
                    path = f"{config.SSO_GW}{path}"
                args = {"path": path, "payload": payload}
                res = page.evaluate(js, args)
                # 🔴 WAF 拦截处理（2026-10-13 重构）：
                #    浏览器 fetch 拿到的 WAF 页可能是
                #      a) 标准 JS 挑战（`acw_sc__v2`，页内含混淆脚本自算 cookie）
                #      b) `aliyun_waf_aa` 滑块页（阿里云验证码 SDK）
                #    两者都**不能只执行第一个 script**（那只定义 arg1/setCookie，
                #    真正算 cookie 的是第二个混淆脚本）。统一做法：把整页
                #    `set_content` 交给浏览器渲染 —— 它自己会执行挑战 JS 写 cookie /
                #    渲染滑块，然后重放 fetch。
                for _ in range(5):
                    txt = res.get("text", "")
                    st = res.get("status")
                    if st not in (200, 405):
                        break
                    is_aa = "aliyun_waf_aa" in txt
                    is_ch = "acw_sc__v2" in txt
                    is_405 = (st == 405 and "405 Not Allowed" in txt)
                    if not (is_aa or is_ch or is_405):
                        break
                    body = txt.replace("document.location.reload()", "void 0;")
                    try:
                        page.set_content(body, wait_until="domcontentloaded")
                    except Exception:
                        break
                    page.wait_for_timeout(1500)
                    if is_aa:
                        _allow["image"] = True      # 让拼图/背景图能加载
                        from .browser.aliyun_slider import solve_aliyun
                        if not solve_aliyun(page):
                            break
                    else:
                        # 等浏览器把挑战/指纹 JS 跑完、cookie 落定
                        d = time.time() + 8
                        while time.time() < d:
                            page.wait_for_timeout(400)
                            if "acw_sc__v2" in {c["name"] for c in ctx.cookies()}:
                                break
                    page.wait_for_timeout(500)
                    res = page.evaluate(js, args)
                return res
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
                try:
                    # 关浏览器并拆掉 playwright 会话（不拆会让后续 Camoufox
                    # 报 "Sync API inside the asyncio loop"）
                    cm.__exit__(None, None, None)
                except Exception:
                    pass

        while True:
            item = self._req_q.get()
            if item is None:            # shutdown：浏览器都是用完即关，直接退出
                return
            tok, (kind, data) = item
            try:
                if kind == "solve":
                    self._resp_q.put((tok, "ok", _do_solve(data)))
                else:
                    path, payload, proxy = data
                    self._resp_q.put((tok, "ok", _do_post(path, payload, proxy)))
            except Exception as ex:     # noqa: BLE001 单次失败不退出循环
                self._resp_q.put((tok, "err", str(ex)))
