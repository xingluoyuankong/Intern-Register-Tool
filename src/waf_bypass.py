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
    chromium_headless_shell → 回退 `config.CHROME_PATH`。
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
    roots = [
        os.path.join(home, "AppData", "Local", "ms-playwright"),
        os.path.join(home, ".cache", "ms-playwright"),
        os.path.join(home, "Library", "Caches", "ms-playwright"),
    ]
    cands: list[str] = []
    for root in roots:
        cands += glob.glob(os.path.join(
            root, "chromium_headless_shell-*", "*", "chrome-headless-shell.exe"))
        cands += glob.glob(os.path.join(
            root, "chromium_headless_shell-*", "chrome-headless-shell-*",
            "chrome-headless-shell"))
        cands += glob.glob(os.path.join(
            root, "chromium_headless_shell-*", "chrome-headless-shell-*",
            "chrome-headless-shell.exe"))
        cands += glob.glob(os.path.join(
            root, "chromium_headless_shell-*", "chrome-linux*", "headless_shell"))
        cands += glob.glob(os.path.join(
            root, "chromium_headless_shell-*", "chrome-mac*", "headless_shell"))
    if cands:
        # 版本号排序取最新（目录名 chromium_headless_shell-<build>）
        return sorted(cands)[-1]
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

    # ── 对外 ─────────────────────────────────────────────────
    def solve(self, challenge_html: str) -> dict[str, str]:
        """[兜底] 算出挑战页要求的 cookie（阻塞，最长 `WAF_SOLVE_TIMEOUT` 秒）。"""
        self._ensure_started()
        self._req_q.put(("solve", challenge_html))
        return self._wait()

    def browser_post(self, path: str, payload: dict,
                     proxy: str = None) -> dict:
        """[主路径] 真实浏览器过 WAF 后，在同一上下文 fetch 注册接口。

        `proxy`：Resin 槽位串（`http://user:@host:port`）—— 每次调用
        **新建 context**（cookie 隔离 + 指定出口），用完即关。与 requests
        的 SSOClient 传同一串，保证两条链路同出口。
        返回 `{"status": int, "text": str}`。阻塞至多 `WAF_SOLVE_TIMEOUT` 秒。
        """
        self._ensure_started()
        self._req_q.put(("post", (path, payload, proxy)))
        return self._wait()

    def shutdown(self):
        """杀 driver 进程树（run.py 收尾时调用；atexit 也会兜一次）。"""
        self._req_q.put(None)

    def _wait(self):
        try:
            kind, val = self._resp_q.get(timeout=config.WAF_SOLVE_TIMEOUT)
        except queue.Empty as ex:
            raise RuntimeError(
                f"WAF 求解超时（>{config.WAF_SOLVE_TIMEOUT}s）") from ex
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
        """playwright 全生命周期都在这个线程（同步 API 的线程绑定约束）。"""
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        atexit.register(kill_driver_tree, pw)
        # 🔴 headless 特征（UA HeadlessChrome / navigator.webdriver）会被
        #    WAF 二层级识别并 405（2026-09-30 实测：首页 JS 能过、fetch 被
        #    拦，UA 覆盖+webdriver 隐藏也不够）→ 改**有头** chromium 跑在
        #    QwenPaw 自带的 Xvfb（DISPLAY=:1）上，特征与真人一致，实测穿透。
        #    完整版 chromium 随 `playwright install chromium` 一起装了。
        # 🔴 Xvfb 会被平台间歇性杀掉：launch 失败不能崩线程 —— 置死标志，
        #    后续请求直接 err（调用方有算法兜底，产线不中断）。
        os.environ.setdefault("DISPLAY", ":1")
        try:
            browser = pw.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"])
        except Exception as ex:  # noqa: BLE001
            self._browser_dead = str(ex)[:200]
            while True:
                item = self._req_q.get()
                if item is None:
                    return
                self._resp_q.put((
                    "err", f"browser unavailable: {self._browser_dead}"))
            return

        def _do_solve(challenge_html: str) -> dict[str, str]:
            """[兜底] 假页面执行挑战 JS，读 cookie（独立 context，用完即关）。"""
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
            """[主路径] 新 context（可带槽位代理）→ 真实过 WAF → 同源 fetch。"""
            kw = {}
            if proxy:
                # 🔴 Chromium 拒绝空密码的代理 URL（ERR_INVALID_AUTH_CREDENTIALS，
                #    2026-09-30 实测），而 Resin 网关**不校验密码**（实测任意密码
                #    同出口）→ 空密码补占位符。
                if ":@" in proxy:
                    proxy = proxy.replace(":/@", ":x@")
                kw["proxy"] = {"server": proxy}
            ctx = browser.new_context(**kw)
            page = ctx.new_page()
            # 🔴 rotating 网关下页面完整资源加载能拖过 90s（图片/字体/统计
            #    脚本各自都要过一次轮换隧道）。WAF 的挑战 JS 是**主文档内联
            #    的**，穿透只需要：主文档 + 脚本 + 我们自己的 fetch。
            #    其余资源全部 abort —— 首页秒开。
            def _trim(route):
                if route.request.resource_type in ("document", "script",
                                                   "xhr", "fetch"):
                    route.continue_()
                else:
                    route.abort()

            page.route("**/*", _trim)
            try:
                page.goto(f"{config.SSO_BASE}/register",
                          wait_until="domcontentloaded", timeout=30000)
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
                args = {"path": path, "payload": payload}
                res = page.evaluate(js, args)
                # 🔴 实测（2026-09-30）：rotating 出口下首个 GET /register 往往
                #    **不触发挑战**（无 acw_sc__v2），首个 fetch POST 才被 WAF
                #    以 405 HTML 挑战（XHR 无法跳转）→ 就地把挑战页的
                #    内联 JS 在当前页面上下文执行（写 cookie），再重放 fetch。
                for _ in range(2):
                    if res.get("status") != 405 or "acw_sc__v2" not in res.get(
                            "text", ""):
                        break
                    m = re.search(r"<script>(.*?)</script>", res["text"], re.S)
                    if not m:
                        break
                    script = m.group(1).replace(
                        "document.location.reload()", "void 0;")
                    page.evaluate(script)
                    page.wait_for_timeout(300)
                    res = page.evaluate(js, args)
                return res
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass

        while True:
            item = self._req_q.get()
            if item is None:            # shutdown：强杀进程树
                kill_driver_tree(pw)
                return
            kind, data = item
            try:
                if kind == "solve":
                    self._resp_q.put(("ok", _do_solve(data)))
                else:
                    path, payload, proxy = data
                    self._resp_q.put(("ok", _do_post(path, payload, proxy)))
            except Exception as ex:     # noqa: BLE001 单次失败不退出循环
                self._resp_q.put(("err", str(ex)))
