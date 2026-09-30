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

import random
import time
from dataclasses import dataclass

import requests

from . import config
from .crypto_rsa import encrypt_password


@dataclass
class RegisterResult:
    ok: bool
    sso_uid: str = ""
    email: str = ""
    username: str = ""
    msg_code: str = ""
    msg: str = ""


class SSOClient:
    def __init__(self, timeout: int = None, proxy: str = None):
        self.gw = config.SSO_GW
        self.timeout = timeout or config.REQUEST_TIMEOUT
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

    def _headers(self, referer_path: str = "/register") -> dict:
        h = dict(self.session.headers)
        h["Referer"] = f"{config.SSO_BASE}{referer_path}"
        return h

    @staticmethod
    def _is_waf_challenge(r: requests.Response) -> bool:
        """阿里云 WAF 的 JS 挑战页（2026-09-30 实测开始拦截写接口）。

        特征：HTTP **200**（不是 403！）+ `text/html` + 页内含
        `acw_sc__v2` / `aliyunwaf`。与正常 JSON 响应（application/json）一眼可分。
        """
        ct = (r.headers.get("content-type") or "").lower()
        return ("text/html" in ct
                and ("acw_sc__v2" in r.text or "aliyunwaf" in r.text))

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

    def _solve_waf(self, r: requests.Response) -> None:
        """解挑战页 → 把 cookie 塞进本 session → 由调用方重放原请求。

        优先**纯算法**（快、无浏览器、同 session 出口一致）；
        arg1 形态不匹配（挑战升级）才退回浏览器求解（waf_bypass）。

        🔴 `acw_tc` 不用手动塞：首发响应的 `set-cookie` 已被 requests 自动
           收进 `self.session.cookies`（探针实测漏带它会被服务端**挂起**）。
        """
        import re as _re

        m = _re.search(r"arg1='([0-9A-Fa-f]{40})'", r.text)
        if m:
            self.session.cookies.set(
                "acw_sc__v2", self._acw_sc_v2(m.group(1)),
                domain="sso.openxlab.org.cn", path="/")
            return
        from .waf_bypass import WafSolver

        cookies = WafSolver.get().solve(r.text)
        for k, v in cookies.items():
            self.session.cookies.set(k, v, domain="sso.openxlab.org.cn", path="/")

    def _post(self, path: str, payload: dict, *, referer: str = "/register",
              attempts: int = 4) -> requests.Response:
        """带退避重试的 POST。

        🔴 为什么必须有：`register/byEmail` 有写操作限流。实测 4 路并发注册时
        3 路立刻拿到 `429 Too Many Requests`（~1.2s 就返回，不是超时）。
        把 429 当注册失败会让批量任务大面积假失败 —— 它只是"慢点再来"。

        ⚠ 2026-09-20 删掉了原先的 `auth: str = None` 形参（连同一个
        `if auth: h["Authorization"] = …` 分支）：它唯一的调用者是已删除的
        `internal_auth()`，删后全仓无调用者传 `auth=`（已 grep 确认）。

        2026-09-30 新增：响应是 WAF 挑战页时，解出 `acw_sc__v2` 塞回 session
        再重放（见 `_solve_waf`）。挑战重放**同样占用 attempts** —— 挑战最多
        应该出现 1 次，连续出现说明 cookie 不被认（IP 换了 / 有效期极短），
        此时多试也无益，如实返回最后一枪让上层归因。
        """
        h = self._headers(referer)
        url = f"{self.gw}{path}"
        last = None
        for i in range(attempts):
            r = self.session.post(url, headers=h, json=payload, timeout=self.timeout)
            if self._is_waf_challenge(r):
                last = r
                if i == attempts - 1:
                    break
                self._solve_waf(r)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = r
                if i == attempts - 1:
                    break
                # 优先用服务端给的 Retry-After，没有就指数退避
                ra = r.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra else 1.5 * (2 ** i)
                except (TypeError, ValueError):
                    delay = 1.5 * (2 ** i)
                delay = min(delay, 20.0) + random.uniform(0, 1.2)
                time.sleep(delay)
                continue
            return r
        last.raise_for_status()
        return last

    # ── 可用性校验 ────────────────────────────────────────────
    def check_username(self, username: str) -> bool:
        """True 表示用户名可用。"""
        r = self._post("/personal/username/check", {"username": username})
        return r.json().get("data") is True

    # ── 注册 ──────────────────────────────────────────────────
    def register(self, username: str, email: str, password: str) -> RegisterResult:
        payload = {
            "username": username,
            "email": email,
            "password": encrypt_password(email, password),
            "source": config.SOURCE,
            "clientId": config.CLIENT_ID,
        }
        r = self._post("/register/byEmail", payload)
        body = r.json()
        data = body.get("data") or {}
        return RegisterResult(
            ok=body.get("success") is True,
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
