"""temp-mail.io 免费临时邮箱后端（2026-10-17 接入）。

为什么需要它
------------
🔴 2026-10-17 WAF 破案（`docs/waf-fingerprint-2026-10-17.md`）：注册 POST
   被阿里云 WAF 按**邮箱域名**弹滑块 —— hotmail.com 烧穿（经它注册过
   400+ 账号）、知名一次性邮箱域全在阿里云库里、gmail/qq/proton 撞域名级
   429。temp.tf 仅有的三个池域（outlook/hotmail/gmail）全灭，注册链路当天
   0 产出，日志全是"JSONDecodeError char 0"（= 16KB 滑块页）。

   temp-mail.io 的域名是**自动生成的冷门域**（yzcalo/lnovic/ruutukf/…），
   同出口同会话实测：`ruutukf.com` 直接注册成功、`gmeenramy/olipii/
   tanpony.com` 都到达业务层 —— 多数不在阿里云一次性域名库。
   ⚠ 它的域名会轮换、也会烧（当天 yzcalo 已 429、lnovic/ooynib 已滑块）。
   可用清单由 `IR_TEMPMAILIO_DOMAINS` 维护（顺序即优先级），
   烧了就改那里的顺序/列表，不用动代码。

API（免鉴权，直连即可，无需代理）：
    GET    /api/v3/domains                     域名清单
    POST   /api/v3/email/new  {name, domain}   建箱 → {email, token}
    GET    /api/v3/email/{email}/messages      邮件列表（body_text/body_html）
    DELETE /api/v3/email/{email}?token=…       删箱（本后端用不到）

接口形状与 `TempTfClient` / `TempMailClient` 完全一致
（`create_mailbox` / `list_mails` / `wait_for_mail` /
`wait_for_activation_link` + `last_error` / `last_polls` /
`last_http_errors`），上游（pipeline）经 `build_mail_client()` 无分支使用。
"""

import json
import random
import re
import string
import time

import requests

from . import config
from .proxypool import slot_alive
from .tempmail import Mail

BASE = "https://api.internal.temp-mail.io/api/v3"


def _extract_links(body: str) -> list[str]:
    """从 HTML/纯文本里抽 URL（与 `temptf._extract_links` 同规则）。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'https?://[^\s"\'<>)\]]+', str(body)):
        u = m.group(0).rstrip(".,;:!?")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _to_epoch_ms(v) -> int:
    """created_at → 毫秒时间戳。兼容 epoch 秒/毫秒与 ISO8601；解析失败返回 0。

    解析失败返回 0 是**安全**的：`wait_for_mail` 只在 `since_ts` 非零时才
    比较 `received_at`，而 pipeline 侧不传 `since_ts`。
    """
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        n = float(v)
        return int(n * 1000) if n < 1e11 else int(n)
    s = str(v).strip()
    try:
        n = float(s)
        return int(n * 1000) if n < 1e11 else int(n)
    except ValueError:
        pass
    try:
        import datetime as _dt
        txt = s.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


class TempMailIoClient:
    """temp-mail.io 后端。`domains` 是按优先级排列的可用域名清单。"""

    def __init__(self, *, domains: list[str] = None, base: str = None,
                 timeout: int = None, session: requests.Session = None):
        self.base = (base or BASE).rstrip("/")
        self.timeout = timeout or config.REQUEST_TIMEOUT
        # 🔴 域名清单：env 顺序优先；没配就问 API 拿当前全量（服务端会轮换）。
        #    直连、关 trust_env —— 收信不该走代理（同 Worker/temp.tf 的理由：
        #    轮询是瓶颈，绕道代理只会更慢）。
        self.domains = [d.strip() for d in (domains or config.TEMPMAILIO_DOMAINS)
                        if d.strip()]
        self.session = session or requests.Session()
        self.session.trust_env = False
        # 🔴 2026-10-19：temp-mail.io 已把本机直连 IP **全量拉黑**：
        #    直连建箱 → 200 但不落库（死箱）；直连读信 → 400 Email not found。
        #    唯一通路 = 代理槽位。⇒ 所有 temp-mail.io 流量（建箱+收信+轮询）
        #    一律走代理槽位（_mail_session），槽位失效自动轮换（_rotate_slot）。
        #    全部槽位死透时才直连兜底（死箱核验会拦住直连假箱）。
        self._slots = []
        try:
            self._slots = config.proxy_slots()
        except Exception:
            self._slots = []
        self._slot_idx = 0
        self.create_slot = None
        self._px_session = self._build_px_session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        # 诊断字段（pipeline 的 stage_register 会读，缺失会 AttributeError）
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0

    # ── 内部 ──────────────────────────────────────────────
    def _build_px_session(self) -> requests.Session:
        """给当前 _slot_idx 建一个走代理槽位的会话（全部槽死→直连兜底）。"""
        s = requests.Session()
        s.trust_env = False
        if self._slots:
            self.create_slot = self._slots[self._slot_idx % len(self._slots)]
            s.proxies = {"http": self.create_slot,
                         "https": self.create_slot}
        else:
            self.create_slot = None
        return s

    def _rotate_slot(self) -> requests.Session:
        """当前槽失效 → 轮换到下一个（最多绕一圈），返回新会话。"""
        if not self._slots:
            return self.session                      # 没配槽位 → 直连
        for _ in range(len(self._slots)):
            self._slot_idx = (self._slot_idx + 1) % len(self._slots)
            s = self._build_px_session()
            if slot_alive(self.create_slot, timeout=5.0):
                return s
        self.create_slot = None
        return self.session                          # 全部死透 → 直连兜底

    def _mail_request(self, method: str, path: str, **kw) -> requests.Response:
        """temp-mail.io 统一入口：走代理槽位，失败自动轮换重试。"""
        for attempt in range(3):
            try:
                r = self._px_session.request(method, f"{self.base}{path}",
                                             timeout=max(self.timeout, 20),
                                             **kw)
                # 400/死箱形态 = 槽位出口已被拉黑 → 换槽重来
                if r.status_code in (400, 403, 429) and self._slots:
                    self._px_session = self._rotate_slot()
                    continue
                return r
            except requests.RequestException:
                if attempt == 2 or not self._slots:
                    raise
                self._px_session = self._rotate_slot()
        raise RuntimeError("temp-mail.io: 代理槽位轮换 3 轮仍失败")

    def _live_domains(self) -> list[str]:
        """当前服务端域名清单；env 清单与它求交后的**有序可用集**。

        🔴 必须求交再排序：env 清单可能过时（域名下架），服务端清单可能
        新上还没测过 WAF 的域。交集 = "env 认可 且 服务端还在"。
        交集为空（env 全过时）→ 退回服务端全量 —— 宁可用新域碰运气，
        也别把整链卡死。
        """
        try:
            # 🔴 2026-10-19 重大修复：必须走代理（self.session 是直连，
            #    而 temp-mail.io 已把本机直连 IP 全量拉黑 → 拉清单失败 →
            #    退回 env 里那 4 个**已被站方批量拉黑**的旧域 ⇒ 永远收不到信。
            #    这就是"换代理建箱了却还是 0 收信"的真因。
            r = self._mail_request("GET", "/domains")
            r.raise_for_status()
            live = [d["name"] for d in (r.json() or {}).get("domains", [])
                    if isinstance(d, dict) and d.get("name")]
        except Exception:
            live = []
        if not self.domains or not live:
            order = self.domains or live
            return list(order)
        inter = [d for d in self.domains if d in live]
        return inter or list(live)

    def _new_email(self, name: str, domain: str) -> dict:
        # 🔴 2026-10-19：统一走 _mail_request（代理槽位+自动轮换）。
        r = self._mail_request("POST", "/email/new",
                               json={"name": name, "domain": domain})
        r.raise_for_status()
        return r.json()

    # ── 对外（与 TempTfClient 同形） ──────────────────────
    def create_mailbox(self, domain: str = None, count: int = 1) -> list[str]:
        """建 N 个邮箱。`domain` 可以是单个域名；None 则按优先级清单轮着用。"""
        out: list[str] = []
        pool = self._live_domains()
        if not pool:
            raise RuntimeError("temp-mail.io 域名清单拉不到（API 不可达？）")
        for i in range(count):
            want = (domain if domain and "@" not in domain else None)
            # 🔴 2026-10-17：多箱**域名轮转** —— 不指定 domain 时随机挑一个：
            #    pipeline 是**每号单独**调 create_mailbox(count=1)，按序号
            #    轮转每次都从 pool[0] 起步 ⇒ 全部落同一个域，集中烧穿域名
            #    信誉（hotmail 就是这么死的）。随机分布 + 4 个域轮换使用。
            if want and want in pool:
                order = [want]
            else:
                random.shuffle(pool)
                order = ([want] + pool) if want else list(pool)
            last_err = ""
            for d in order:
                name = "".join(random.choices(string.ascii_lowercase + string.digits,
                                              k=12))
                try:
                    j = self._new_email(name, d)
                    email = j.get("email")
                    if not email:
                        last_err = f"{d}: no-email-in-response"
                        continue
                    # 🔴 2026-10-19：死箱核验 —— 裸建箱可能「200 但不落库」，
                    #    1s 后必须能查（空列表也算健康）才收货，否则换域重造。
                    time.sleep(1.0)
                    try:
                        self.list_mails(email)
                    except requests.RequestException:
                        last_err = f"{d}: dead-mailbox(recreated)"
                        continue
                    out.append(email)
                    break
                except requests.RequestException as ex:
                    # 域名建不了（下架/参数错）→ 换下一个域；真没域名可换才抛
                    last_err = f"{d}: {type(ex).__name__}"
            if len(out) <= i:
                raise RuntimeError(f"temp-mail.io 建箱失败（最后尝试 {last_err}）")
        return out

    def list_mails(self, email: str, limit: int = None) -> list[Mail]:
        """列出某个地址的邮件。404 = 还没有这个地址的记录（视为空）。"""
        if not email:
            return []
        # 🔴 2026-10-19：读信也走代理（直连 IP 已被 temp-mail.io 拉黑）。
        r = self._mail_request("GET", f"/email/{email}/messages")
        if r.status_code == 404:
            return []          # 没记录 ≠ 读失败
        r.raise_for_status()
        raw = r.json() or []
        mails = [self._to_mail(m, email) for m in raw if isinstance(m, dict)]
        return mails[:limit] if limit else mails

    @staticmethod
    def _to_mail(m: dict, fallback_to: str) -> Mail:
        body = m.get("body_html") or m.get("body_text") or ""
        # 优先 html（激活链接在 <a> 里），text 兜底
        if not body:
            body = m.get("body_text") or ""
        return Mail(
            id=str(m.get("id", "")),
            to_address=str(m.get("to") or fallback_to),
            from_address=str(m.get("from") or ""),
            subject=str(m.get("subject") or ""),
            body=str(body),
            # `Mail.links` 吃这个字段 —— 形状与 temptf._to_mail 一致
            extracted_json=json.dumps(
                [{"value": u} for u in _extract_links(str(body))]),
            received_at=_to_epoch_ms(m.get("created_at")),
        )

    def wait_for_mail(self, address: str, sender_contains: str = "openxlab",
                      timeout: int = None, interval: float = None,
                      since_ts: int = 0, limit: int = None) -> "Mail | None":
        """轮询等到目标邮件（语义同 `TempTfClient.wait_for_mail`）。

        `sender_contains` 只作软过滤（from 可能是代发），先按关键词找，
        找不到退回"该地址收到的第一封"。
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
        fallback: "Mail | None" = None

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
            # 激活邮件理论上只此一封 —— 有信就别再轮询
            if fallback is not None:
                return fallback
            time.sleep(interval)

        if errs:
            self.last_error = (f"temp-mail.io 持续不可读（{errs} 次，最近 {last_code}）"
                               f" —— 不是邮件没到，是接口读不出来")
        return None

    def wait_for_activation_link(self, address: str, **kw) -> "str | None":
        m = self.wait_for_mail(address, **kw)
        if not m:
            return None
        return m.find_link("active", "activat", "verif", "confirm")
