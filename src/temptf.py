"""temp.tf 临时邮箱后端（CF Worker 之外的**第二邮箱源**）。

为什么要有它：`src/tempmail.py` 依赖自建的 CF Worker 邮箱服务
（`IR_WORKER_BASE` / `IR_WORKER_ADMIN_TOKEN` / `IR_WORKER_DOMAIN` 三项必填）。
手上没有那个 Worker 时，整条链路在启动阶段就被 `config.validate()` 拦死。
temp.tf 提供真实 outlook / hotmail / gmail 别名收件箱，免注册、有 HTTP API，
且**域名是真实大厂域名**（目标站一般不拦），所以拿它顶上。

实测到的 API（2026-09-30，从 `https://temp.tf/_next/static/chunks/app/page-*.js`
抽出来的，不是猜的）：

    GET  /api/account?providers=outlook&dot=0&plus=1   -> {"email": "..."}
    POST /api/check  {"email": "...", "wait": false}   -> {"data": [...], "totalReceived": N}
    GET  /api/stats?providers=outlook                  -> 池子规模
    GET  /api/attachment?...                           -> 附件（本项目不用）

邮件对象字段（同 chunk 的渲染代码里读出来的）：
    id, from, to, subject, body, bodyContentType("html"|"text"),
    date, attachments, inlineCids

🔴 与 CF Worker 后端的**关键差异**：Worker 侧做了「激活链接自动提取」，
   直接给 `extracted_json`；temp.tf **不给** —— 只有原始 HTML 正文。
   所以本模块自己正则抽链接，再**回填成 `extracted_json` 的形状**，
   让 `tempmail.Mail.links` / `find_link()` 原样可用。这样上游
   （`pipeline.stage_register`）一行都不用改。

限流：`/api/account` 会返回 429 + `Retry-After`（并发建箱时实测到）。
建箱按 provider 顺序往下退（outlook → hotmail → gmail），撞 429 就换下一个。
"""

import json
import re
import time

import requests

from . import config
from .tempmail import Mail

# provider 名 -> temp.tf 的 providers 参数值
# 键是「邮箱域名」（run.py --mail-domain 传进来的就是这个），值是 API 用的名。
DOMAIN_TO_PROVIDER = {
    "outlook.com": "outlook",
    "hotmail.com": "hotmail",
    "gmail.com": "gmail",
    "high.edu.pl": "highEduPl",
}

# 建箱时的默认尝试顺序（实测 outlook 池子最大：25 个账号 / hotmail 26 / gmail 5）
DEFAULT_PROVIDERS = ["outlook", "hotmail", "gmail"]

_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\'>]+)["\']', re.I)
# 正文里的裸 URL（HTML 被剥成纯文本时靠它）
_BARE_URL_RE = re.compile(r'https?://[^\s<>"\'）)\]]+', re.I)


def _extract_links(body: str) -> list[str]:
    """从邮件正文里抽出候选链接（去实体、去重、保序）。

    顺序有讲究：`find_link()` 按关键词匹配后**返回第一个**，所以激活链接
    必须排在前面。真实激活邮件里它通常在 HTML 的上半部分（按钮区），
    href 先扫一遍基本就命中了；裸 URL 兜底放在后面。
    """
    if not body:
        return []
    text = (body.replace("&amp;", "&")
                .replace("&#61;", "=")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&#39;", "'"))
    out: list[str] = []
    seen = set()

    def add(u: str):
        u = u.strip().rstrip(".,;")
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    for m in _HREF_RE.finditer(text):
        u = m.group(1)
        if u.lower().startswith(("http://", "https://")):
            add(u)
    for m in _BARE_URL_RE.finditer(text):
        add(m.group(0))
    return out


def _to_epoch_ms(v) -> int:
    """把各种日期表示统一成**毫秒** epoch（`pipeline` 靠 `> 1e11` 判毫秒）。

    实测 temp.tf 的 `date` 是 ISO 串；CF Worker 那边是毫秒整数。两边都要吃得下。
    """
    if isinstance(v, (int, float)) and v:
        v = int(v)
        return v if v > 1e11 else v * 1000
    if isinstance(v, str) and v:
        s = v.strip()
        # 纯数字串
        if s.isdigit():
            n = int(s)
            return n if n > 1e11 else n * 1000
        # ISO 8601（带 Z / 带时区 / 不带时区）
        try:
            import datetime as _dt
            iso = s.replace("Z", "+00:00")
            d = _dt.datetime.fromisoformat(iso)
            if d.tzinfo is None:
                d = d.replace(tzinfo=_dt.UTC)
            return int(d.timestamp() * 1000)
        except ValueError:
            pass
    return 0


class TempTfClient:
    """temp.tf 客户端，接口形状与 `tempmail.TempMailClient` 对齐。"""

    def __init__(self, base: str = None, timeout: int = None,
                 providers: list[str] = None, session: requests.Session = None):
        self.base = (base or config.TEMPTF_BASE).rstrip("/")
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.providers = list(providers or config.TEMPTF_PROVIDERS)
        self.session = session or requests.Session()
        # 与 CF Worker 后端一致的开关：邮箱侧默认不走代理（Cloudflare/大厂收信
        # 不关心出口 IP，绕代理只会让轮询更慢）。要开就 IR_PROXY_MAIL=1。
        if config.MAIL_PROXY:
            config.apply_proxy(self.session)
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": config.USER_AGENT,
            "Origin": self.base,
            "Referer": f"{self.base}/",
        })
        # 与 TempMailClient 同名的三个诊断字段（pipeline 会读它们做归因）
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0

    # ── 建箱 ────────────────────────────────────────────────
    def create_mailbox(self, domain: str = None, count: int = 1) -> list[str]:
        """按 provider 顺序建 N 个邮箱。

        `domain` 可以是 `outlook.com` 这类域名（映射到对应 provider），
        也可以是 provider 名本身；都认不出来就按默认顺序逐个降级尝试。
        """
        order = self._provider_order(domain)
        out: list[str] = []
        for _ in range(count):
            addr = None
            last_err = ""
            for prov in order:
                try:
                    addr = self._new_address(prov)
                except RuntimeError as ex:
                    last_err = str(ex)     # 429 等：换下一个 provider
                    continue
                if addr:
                    break
            if not addr:
                raise RuntimeError(f"temp.tf 建箱失败（providers={order}）：{last_err}")
            out.append(addr)
        return out

    def _provider_order(self, domain: str) -> list[str]:
        if domain:
            d = domain.strip().lower()
            prov = DOMAIN_TO_PROVIDER.get(d) or (d if d in DOMAIN_TO_PROVIDER.values() else "")
            if prov:
                # 指定的放最前，其余兜底
                rest = [p for p in self.providers if p != prov]
                return [prov] + rest
        return list(self.providers) or list(DEFAULT_PROVIDERS)

    def _new_address(self, provider: str) -> str:
        r = self.session.get(
            f"{self.base}/api/account",
            params={"providers": provider, "dot": "0", "plus": "1"},
            timeout=self.timeout,
        )
        # 429 = 建箱限流（Retry-After 给出秒数）。不是致命错误 —— 换 provider 再试。
        if r.status_code == 429:
            raise RuntimeError(f"429 (retry-after {r.headers.get('Retry-After')})")
        r.raise_for_status()
        email = (r.json() or {}).get("email", "")
        if not email:
            raise RuntimeError(f"empty email: {r.text[:200]}")
        return str(email).strip().lower()

    # ── 收信 ────────────────────────────────────────────────
    def list_mails(self, email: str, limit: int = None) -> list[Mail]:
        """列出某个地址的邮件（`POST /api/check`，服务端按地址过滤）。"""
        r = self.session.post(
            f"{self.base}/api/check",
            json={"email": email, "wait": False},
            timeout=max(self.timeout, 45),   # wait=false 也可能走一次上游 IMAP 同步
        )
        r.raise_for_status()
        raw = (r.json() or {}).get("data") or []
        return [self._to_mail(m, email) for m in raw if isinstance(m, dict)]

    @staticmethod
    def _to_mail(m: dict, fallback_to: str) -> Mail:
        body = (m.get("body") or m.get("html") or m.get("text") or "")
        links = _extract_links(str(body))
        return Mail(
            id=str(m.get("id", "")),
            to_address=str(m.get("to") or m.get("to_address") or fallback_to or ""),
            from_address=str(m.get("from") or m.get("from_address") or ""),
            subject=str(m.get("subject") or ""),
            body=str(body),
            # 🔴 回填成 Worker 的形状 —— `Mail.links` 直接吃这个字段
            extracted_json=json.dumps([{"value": u} for u in links]),
            received_at=_to_epoch_ms(m.get("date") or m.get("receivedAt")
                                     or m.get("received_at")),
        )

    def wait_for_mail(
        self,
        address: str,
        sender_contains: str = "openxlab",
        timeout: int = None,
        interval: float = None,
        since_ts: int = 0,
        limit: int = None,
    ) -> Mail | None:
        """轮询等到目标邮件（语义与`TempMailClient.wait_for_mail`一致）。

        `sender_contains` 默认 "openxlab"，但**只作软过滤**：temp.tf 返回的
        `from` 可能是 display name 或转发地址（实测有些站点用代发服务，
        from 里不含站点名）。硬过滤会把真邮件漏掉 —— 所以先按关键词找，
        找不到时**退回"该地址收到的第一封"**，而不是判失败。
        """
        timeout = timeout or config.MAIL_POLL_TIMEOUT
        interval = interval or config.MAIL_POLL_INTERVAL
        deadline = time.time() + timeout
        target = address.lower()
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        errs = 0
        last_code = ""
        fallback: Mail | None = None

        while time.time() < deadline:
            self.last_polls += 1
            try:
                mails = self.list_mails(target)
            except requests.HTTPError as ex:
                code = ex.response.status_code if ex.response is not None else 0
                if code and code < 500:
                    raise
                errs += 1
                self.last_http_errors = errs
                last_code = str(code or "?")
                time.sleep(min(interval * (1 + errs // 10), 2.0))
                continue
            except requests.RequestException:
                # 网络抖动（连接重置 / 超时）：跟 5xx 一样是"现在读不出来"，重试。
                errs += 1
                self.last_http_errors = errs
                last_code = "network"
                time.sleep(min(interval * (1 + errs // 10), 2.0))
                continue

            for m in mails:
                if m.to_address and m.to_address.lower() != target:
                    continue
                if since_ts and m.received_at and m.received_at < since_ts:
                    continue
                if sender_contains and sender_contains.lower() in m.from_address.lower():
                    return m
                fallback = fallback or m
            # 该地址下**只可能有一封激活邮件** —— 有信就别再轮询了。
            if fallback is not None:
                return fallback
            time.sleep(interval)

        if errs:
            self.last_error = (f"temp.tf 持续不可读（{errs} 次，最近 {last_code}）"
                               f"—— 不是邮件没到，是接口读不出来")
        return None

    def wait_for_activation_link(self, address: str, **kw) -> str | None:
        mail = self.wait_for_mail(address, **kw)
        if not mail:
            return None
        return mail.find_link("active", "activat", "verif", "confirm")
