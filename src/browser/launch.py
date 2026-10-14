"""浏览器启动层 —— Camoufox（指纹浏览器）。

2026-10-02 从 Playwright/Chrome 切到 **Camoufox**，原因（用户指定）：
  - 指纹随机化：每次启动都是**全新指纹**（全套注入）
  - `geoip=True`：时区 / 语言 / 地区**自动跟随代理出口 IP**
  - `proxy` 支持**带认证**的 dict（server + username + password）
  - 开源、成本零、Python API（自动化最强）

🔴 与 Chrome 的关键差异（改动时必须知道）：
  Firefox/Camoufox 的代理是**启动级**参数，**不支持 per-context 换出口**。
  所以「一个账号一个出口」必须**每次新建浏览器实例**；
  `BrowserSession` 的「复用浏览器只换 context」只在**不换出口**时成立。

🔴 缓存： Camoufox 默认每次用全新临时 profile；再叠
  `enable_cache=False` + `browser.cache.*=False`，保证关闭即弃、不留缓存。
"""

from .attempt import _playwright_proxy

import os
import subprocess
import time

# 🔴 Windows + 沙箱/受限会话下（WorkBuddy 终端、SSH 服务会话等）：
#    Firefox GPU 进程默认开 sandbox，会在受限环境里拿不到 D3D11 设备 →
#    `FEATURE_FAILURE_D3D11_NO_DEVICE` → RenderCompositorSWGL 崩 →
#    `new_page()` 报 "Target page, context or browser has been closed"。
#    实测（2026-10-03）：注入 MOZ_DISABLE_GPU_SANDBOX / MOZ_DISABLE_CONTENT_SANDBOX
#    后 headful 模式完全正常（裸跑与 Playwright 驱动均验证）。
#    headless=True 依旧必崩（SWGL 无 dt 与沙箱无关，是 -headless 的
#    离屏合成缺陷），所以 Windows 本机一律走「headful + 窗口移出屏幕」。
def _apply_camoufox_env() -> None:
    os.environ.setdefault("MOZ_DISABLE_GPU_SANDBOX", "1")
    os.environ.setdefault("MOZ_DISABLE_CONTENT_SANDBOX", "1")


def _offscreen_window(window: tuple) -> tuple:
    """把窗口位置移到屏幕外（Playwright Firefox 用 `--window x,y` 不可行，
    Camoufox 不暴露 position → 用 prefs 的 `browser.window.position` 无法
    直接设置；改用最小尺寸 + 依赖启动机保持最小化。返回原 window。
    真正移屏靠 `launch_options` 的 args `-window` 无法与 playwright 共存，
    因此这里只把窗口调小，用户若在意闪烁可自行最小化。"""
    return (min(window[0], 480), min(window[1], 360))


# 指纹浏览器的默认启动选项
def _resolve_exit_ip(proxy: str = None, timeout: int = 15) -> str | None:
    """解析代理出口的公网 IP（给 geoip 用）。

    🔴 为什么不直接用 `geoip=True` 让 Camoufox 自己查：它内部
       `public_ip(proxy)` 对每个候选服务只给 **5 秒**，而槽位代理到
       `ipecho.net` 这类站点实测读超时（代理商没放行这些域名）→ 抛
       `camoufox.exceptions.InvalidIP`，浏览器直接起不来。
       这里自己查（宽超时 + 多候选），查不到就返回 None（调用方降级
       `geoip=False`，指纹仍然随机，只是时区/语言不再跟 IP）。
    """
    import re

    import requests

    urls = ("https://api.ipify.org", "https://checkip.amazonaws.com",
            "https://ipinfo.io/ip", "https://icanhazip.com",
            "https://ifconfig.co/ip")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    for u in urls:
        try:
            r = requests.get(u, proxies=proxies, timeout=timeout)
            ip = r.text.strip()
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
                return ip
        except Exception:  # noqa: BLE001 候选逐个试，全挂才放弃
            continue
    return None


def camoufox_options(proxy: str = None, *, headless: bool = False,
                     window: tuple = (1280, 900),
                     os_name: str = "windows") -> dict:
    """组装 `Camoufox(**opts)` 的参数。

    proxy:   Resin 槽位串（`http://user:pass@host:port`），带认证。
             Camoufox 需要拆成 dict（和 Playwright 一样不接受 URL 内嵌认证）。
    geoip:   自动把时区/语言/地理位置对齐到代理出口 IP —— 这是「指纹跟 IP」
             的关键，避免「美国 IP + 中国时区」这类矛盾指纹。
    """
    opts = {
        "headless": headless,
        # 指纹 OS 与本机一致，避免 UA/平台指纹自相矛盾
        "os": os_name,
        # 🔴 时区 / 语言 / 地理跟随出口 IP —— 传 **IP 字符串**（不是 True）：
        #    传 True 会让它自己去查公网 IP（5s 超时，经槽位代理必挂）。
        #    查不到就降级 False，指纹仍随机，只是时区不再跟 IP。
        "geoip": _resolve_exit_ip(proxy) or False,
        # 每次全新：不复用任何缓存
        "enable_cache": False,
        "window": window,
        "firefox_user_prefs": {
            "browser.cache.disk.enable": False,
            "browser.cache.memory.enable": False,
            "browser.cache.disk_cache_ssl": False,
            "browser.cache.offline.enable": False,
            "network.http.use-cache": False,
            # 关闭即弃，不写历史/表单/会话恢复
            "browser.sessionstore.resume_from_crash": False,
            "browser.formfill.enable": False,
            # 🔴 磁盘泄漏根治（2026-10-04 实测）：只关 HTTP 磁盘缓存不够 ——
            #    Firefox 照样往 profile 里写 startupCache(18M) / places.sqlite
            #    历史库(5M) / favicons.sqlite(5M) / cookies.sqlite / logins.db，
            #    一轮一个 profile，异常退出还不删 → /tmp 堆到 282M、残留 8 个。
            #    这几项把"写盘"本身关掉，profile 就只剩骨架。
            "places.history.enabled": False,
            "browser.places.importBookmarks": False,
            "browser.bookmarks.max_backups": 0,
            "signon.rememberSignons": False,
            "signon.autofillForms": False,
            "browser.shell.checkDefaultBrowser": False,
            "browser.startup.page": 0,
            "browser.startup.homepage_override.mstone": "ignore",
            "permissions.memory_only": True,
            "dom.push.enabled": False,
            "browser.uitour.enabled": False,
        },
    }
    if proxy:
        opts["proxy"] = _playwright_proxy(proxy)
        # 🔴 Resin 槽位串是 `http://user:@host:port`（**空密码**）。Camoufox 的
        #    geoip 会用 `Proxy(**proxy).as_string()` 拼回 URL 去查出口 IP，空密码
        #    会让代理回 407（`Tunnel connection failed: 407 Proxy Authentication
        #    Required`）。网关不校验密码 → 补占位符（与 Chrome 路径同款处理）。
        if opts["proxy"].get("username") and not opts["proxy"].get("password"):
            opts["proxy"]["password"] = "x"
    return opts


def launch_camoufox(proxy: str = None, *, headless: bool = False,
                    window: tuple = (1280, 900)):
    """返回 `Camoufox` 上下文管理器（`with ... as browser`）。

    ⚠ 用 `with` 包起来：退出时它会 close 浏览器并拆掉 playwright 会话；
    不 close 会让同一线程后续 `Camoufox()` 全部报
    "Sync API inside the asyncio loop"。

    🔴 Windows 本机（2026-10-03 定案）：
      - `headless=True` 必崩（SWGL 无 dt，与沙箱无关，改 prefs 无效）；
      - `headless=False` + 禁 GPU/内容沙箱环境变量 = 完全可用。
      本机跑批时：`IR_HEADFUL_FORCED=1` 自动把 headless 转成小窗 headful。
    """
    _apply_camoufox_env()
    force_headful = os.getenv("IR_HEADFUL_FORCED", "").strip() in ("1", "true", "yes")
    # 🔴 2026-10-13 容器（Linux）：headful 依赖 Xvfb，X 一抖浏览器就
    #    "Failed to launch" / "Target closed"（实测成主要损耗来源）。
    #    Linux 上 headless 是官方支持的（Windows 的"headless 必崩"不适用），
    #    用 `IR_BROWSER_HEADLESS=1` 强制无头 —— 滑块拖动/鼠标事件无头照常工作。
    if (os.name != "nt" and not headless
            and os.getenv("IR_BROWSER_HEADLESS", "").strip() in ("1", "true", "yes")):
        headless = True
    if os.name == "nt" and headless:
        # Windows 下 headless 必崩 → 若未显式允许，转成小窗 headful
        if force_headful or os.getenv("IR_HEADFUL_AUTO", "1") != "0":
            headless = False
            window = _offscreen_window(window)
    from camoufox.sync_api import Camoufox

    return Camoufox(**camoufox_options(proxy, headless=headless, window=window))


class _ChromeCtx:
    """回退引擎：Playwright/Chromium（本机 Windows 上 Camoufox 开不了页时的退路）。

    🔴 2026-10-03 实测：本机图形栈 D3D11 不可用
       （`FEATURE_FAILURE_D3D11_NO_DEVICE` → `RenderCompositorSWGL failed mapping
       default framebuffer`）→ Camoufox/Firefox 在 `new_page()` 必崩。
       手动跑 `camoufox.exe` 能起（退出码 0），但经 Playwright 驱动就不行。
       Linux 服务器有 Xvfb 不受此影响（那边 `headless="virtual"` 可用）。
    """

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self._browser = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        return self._browser

    def __exit__(self, *exc):
        from ..waf_bypass import kill_driver_tree

        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        if self._pw is not None:
            kill_driver_tree(self._pw)
        return False


def current_engine() -> str:
    """当前引擎名（`camoufox` / `chrome`）。

    `_run_attempt(fingerprint_browser=...)` 要跟着它走：`no_viewport` 是
    **Firefox 专属**参数，Chrome 引擎下传 True 会直接报错。
    """
    import os

    e = os.getenv("IR_BROWSER_ENGINE", "camoufox").strip().lower()
    if e in ("chrome", "chromium", "playwright"):
        return "chrome"
    return "camoufox"


def launch_browser(proxy: str = None, *, headless: bool = False,
                   window: tuple = (1280, 900)):
    """按 `IR_BROWSER_ENGINE` 选引擎，返回上下文管理器。

    - `camoufox`（默认）：指纹浏览器，全新指纹 + geoip 跟 IP + 代理带认证。
    - `chrome`：回退到 Playwright/Chromium。
    """
    if current_engine() == "chrome":
        cleanup_stale_profiles()
        return _ChromeCtx(headless=headless)
    cleanup_stale_profiles()      # 每次启动前扫一遍陈旧 profile（防 /tmp 泄漏）
    return launch_camoufox(proxy, headless=headless, window=window)


def cleanup_stale_profiles(max_age_min: int = 30) -> int:
    """删掉**没被任何进程占用**的浏览器临时 profile（防 /tmp 泄漏）。

    🔴 为什么必须兜底（2026-10-04 服务器实测）：
       浏览器走异常路径（TargetClosedError / 超时 / 被 SIGKILL）时，
       Playwright 的 `browser.close()` 根本没机会跑，临时 profile 就永远
       留在 /tmp —— 一轮一个，实测堆积到 8 个目录 / 282M，单个最大 77M
       （44M storage + 18M startupCache + 历史/图标/cookie 库）。
       注册是无限循环，这点泄漏跑一天就吃满磁盘。

    安全约束：先用 `fuser` 判断占用，**绝不删正在被活浏览器使用的目录**
    （删了会让当前这一轮直接崩）。
    """
    import glob as _glob
    import shutil

    patterns = ("/tmp/playwright_firefoxdev_profile-*",
                "/tmp/playwright_chromiumdev_profile-*",
                "/tmp/camoufox*", "/tmp/.org.chromium*")
    now, removed = time.time(), 0
    for pat in patterns:
        for d in _glob.glob(pat):
            try:
                if not os.path.isdir(d):
                    continue
                if os.name == "nt":
                    continue            # Windows 下 profile 路径不同，交给系统
                # 占用中 → 跳过（fuser 返回 0 = 有进程在用）
                if subprocess.run(["fuser", d], capture_output=True,
                                  timeout=5, check=False).returncode == 0:
                    continue
                if (now - os.path.getmtime(d)) < max_age_min * 60:
                    continue
                shutil.rmtree(d, ignore_errors=True)
                if not os.path.exists(d):
                    removed += 1
            except Exception:
                continue
    return removed
