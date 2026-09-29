"""temp.tf 后端的行为测试（**全离线** —— 用假 session，不打真实接口）。

为什么要这些用例：`src/temptf.py` 里有三处是**靠推断**写出来的、且错了不会
抛异常只会静默走错分支的地方 —— 它们的失败形态都是"注册跑到一半卡住"，
排查成本很高：

1. **链接提取**（`_extract_links`）：temp.tf 不像 CF Worker 那样给提取好的
   `extracted_json`，只有原始 HTML。抽不出来 ⇒ `find_link()` 返回 None ⇒
   `stage_register` 报 "activation link not found"，而邮件其实早就到了。
   🔴 真实激活邮件里的 URL 带 `&amp;` 实体和 `=`（query 串），
   不做实体还原就抽不出完整链接 —— 这条必须有断言钉住。
2. **时间归一**（`_to_epoch_ms`）：`pipeline` 靠 `> 1e11` 判毫秒。
   temp.tf 给 ISO 串、CF Worker 给毫秒整数，两种都要吃得下。
   解析错了不会崩，只会让 `arrival_delay_ms` 变成天文数字 —— 静默污染计时。
3. **发件人软过滤**：`wait_for_mail` 默认 `sender_contains="openxlab"`，
   但 temp.tf 的 `from` 可能是代发服务（`from` 里没有 openxlab 字样）。
   硬过滤会把真邮件判成"没收到"。所以实现是"先按关键词找，找不到退回第一封"。
   这条如果退化成硬过滤，症状是**偶发**丢邮件，最难查。

另外钉住的两个契约：
- `create_mailbox` 撞 429 时按 provider 顺序降级（不是直接抛）
- `build_mail_client()` 按 `IR_MAIL_PROVIDER` 选后端（配了开关就得生效）
"""

from __future__ import annotations

import json

import pytest

from src import config
from src.tempmail import build_mail_client
from src.temptf import TempTfClient, _extract_links, _to_epoch_ms


class _Resp:
    """够用的 `requests.Response` 替身。"""

    def __init__(self, payload=None, status=200, text="", headers=None):
        self._payload = payload
        self.status_code = status
        self.text = text or json.dumps(payload or {})
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code), response=self)


class _Session:
    """记录调用、按脚本返回响应的假 session。"""

    def __init__(self, get_responses=None, post_responses=None):
        self.headers = {}
        self.gets: list[dict] = []
        self.posts: list[dict] = []
        self._get_responses = list(get_responses or [])
        self._post_responses = list(post_responses or [])

    def get(self, url, params=None, timeout=None):
        self.gets.append({"url": url, "params": params})
        return self._get_responses.pop(0) if self._get_responses else _Resp({})

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        return self._post_responses.pop(0) if self._post_responses else _Resp({"data": []})


# ── 链接提取 ─────────────────────────────────────────────────────


def test_extract_links_unescapes_amp_entities():
    """`&amp;` 必须还原成 `&` —— 否则带 query 串的激活链接会被截断。

    这是整个后端最容易静默失效的一点：URL 拿回来少半截，请求照样发出去，
    只是激活永远返回 false。
    """
    html = ('<a href="https://sso.openxlab.org.cn/active?token=abc&amp;uid=42">'
            "点此激活</a>")
    links = _extract_links(html)
    assert "https://sso.openxlab.org.cn/active?token=abc&uid=42" in links


def test_extract_links_keeps_activation_first():
    """激活链接要排在前面 —— `find_link()` 返回**第一个**匹配的。

    真实邮件底部有 unsubscribe / 隐私政策等一堆外链，顺序反了就会点到退订链接。
    """
    html = ('<a href="https://example.com/unsubscribe">退订</a>'
            '<a href="https://sso.openxlab.org.cn/activate?t=1">激活</a>')
    links = _extract_links(html)
    assert links[0].endswith("/unsubscribe")
    assert any("activate" in u for u in links)


def test_extract_links_falls_back_to_bare_url_in_plain_text():
    """HTML 被剥成纯文本时，href 扫不到 —— 裸 URL 兜底必须还在。"""
    links = _extract_links("请访问 https://sso.openxlab.org.cn/activate?t=9 完成激活")
    assert "https://sso.openxlab.org.cn/activate?t=9" in links


def test_extract_links_dedupes_and_tolerates_empty():
    assert _extract_links("") == []
    html = '<a href="https://a.example/x">1</a><a href="https://a.example/x">2</a>'
    assert _extract_links(html).count("https://a.example/x") == 1


# ── 时间归一 ─────────────────────────────────────────────────────


def test_epoch_ms_accepts_seconds_and_millis():
    """两种整数都要认：>1e11 当毫秒，否则当秒。"""
    assert _to_epoch_ms(1789449135216) == 1789449135216
    assert _to_epoch_ms(1790733135) == 1790733135000


def test_epoch_ms_accepts_iso_and_numeric_strings():
    # `2026-09-30T01:52:15Z` 的 epoch 秒 = 1790733135（对照 timegm 手算过）
    assert _to_epoch_ms("2026-09-30T01:52:15Z") == 1790733135000
    assert _to_epoch_ms("1789449135216") == 1789449135216


def test_epoch_ms_returns_zero_for_garbage():
    """解析不了就给 0 —— `pipeline` 里 `received_at` 为 0 只是不记到达延迟，
    不能让它抛异常把整条注册判死。"""
    assert _to_epoch_ms("not a date") == 0
    assert _to_epoch_ms(None) == 0


# ── 建箱 ─────────────────────────────────────────────────────────


def test_create_mailbox_falls_back_to_next_provider_on_429():
    """outlook 撞 429 → 换 hotmail，而不是把整批判死。"""
    sess = _Session(get_responses=[
        _Resp({}, status=429, headers={"Retry-After": "3"}),
        _Resp({"email": "user+abc@hotmail.com"}),
    ])
    c = TempTfClient(session=sess, providers=["outlook", "hotmail", "gmail"])
    assert c.create_mailbox(count=1) == ["user+abc@hotmail.com"]
    assert sess.gets[0]["params"]["providers"] == "outlook"
    assert sess.gets[1]["params"]["providers"] == "hotmail"


def test_create_mailbox_raises_when_all_providers_exhausted():
    sess = _Session(get_responses=[_Resp({}, status=429), _Resp({}, status=429)])
    c = TempTfClient(session=sess, providers=["outlook", "hotmail"])
    with pytest.raises(RuntimeError):
        c.create_mailbox(count=1)


def test_domain_maps_to_provider_and_is_tried_first():
    """`--mail-domain outlook.com` 应该把 outlook 顶到最前（其余仍作兜底）。"""
    sess = _Session(get_responses=[_Resp({"email": "u+x@outlook.com"})])
    c = TempTfClient(session=sess, providers=["outlook", "hotmail", "gmail"])
    c.create_mailbox(domain="hotmail.com", count=1)
    assert sess.gets[0]["params"]["providers"] == "hotmail"


# ── 收信 ─────────────────────────────────────────────────────────


def test_list_mails_maps_temp_tf_fields_into_Mail():
    """字段映射 + `extracted_json` 回填 —— `Mail.links` 必须能直接用。"""
    sess = _Session(post_responses=[_Resp({"data": [{
        "id": "7",
        "from": "noreply@openxlab.org.cn",
        "to": "user+abc@outlook.com",
        "subject": "激活账号",
        "body": '<a href="https://sso.openxlab.org.cn/activate?t=1">激活</a>',
        "bodyContentType": "html",
        "date": "2026-09-30T01:52:15Z",
    }]})])
    c = TempTfClient(session=sess)
    mails = c.list_mails("user+abc@outlook.com")
    assert len(mails) == 1
    m = mails[0]
    assert m.to_address == "user+abc@outlook.com"
    assert m.from_address == "noreply@openxlab.org.cn"
    assert m.find_link("activat") == "https://sso.openxlab.org.cn/activate?t=1"
    assert m.received_at > 1e11


def test_wait_for_mail_accepts_relayed_sender():
    """🔴 软过滤：`from` 里没有 openxlab（代发）时**不能**判成没收到。"""
    sess = _Session(post_responses=[_Resp({"data": [{
        "id": "1",
        "from": "bounces@sendgrid.net",
        "to": "user+abc@outlook.com",
        "subject": "verify",
        "body": '<a href="https://sso.openxlab.org.cn/verify?t=2">go</a>',
        "date": "2026-09-30T01:52:15Z",
    }]})])
    c = TempTfClient(session=sess)
    m = c.wait_for_mail("user+abc@outlook.com", interval=0)
    assert m is not None, "代发邮件被硬过滤掉了 —— 症状是偶发丢激活"
    assert m.find_link("verif") == "https://sso.openxlab.org.cn/verify?t=2"


def test_wait_for_mail_returns_none_only_when_truly_empty():
    """一封都没有 → None（让上游报"邮件没到"，而不是"链接没找到"）。"""
    c = TempTfClient(session=_Session(post_responses=[_Resp({"data": []})]))
    assert c.wait_for_mail("nobody@outlook.com", timeout=0, interval=0) is None


# ── 工厂 / 开关 ──────────────────────────────────────────────────


def test_build_mail_client_honours_provider_switch(monkeypatch):
    """配了 `IR_MAIL_PROVIDER=temptf` 就得真的换后端。

    没有这条，开关配错时症状是"配置看着对了但还在打 CF Worker" —— 
    启动阶段才报、且报的是凭据缺失，很难联想到开关没生效。
    """
    monkeypatch.setattr(config, "MAIL_PROVIDER", "temptf")
    assert isinstance(build_mail_client(), TempTfClient)

    monkeypatch.setattr(config, "MAIL_PROVIDER", "worker")
    assert not isinstance(build_mail_client(), TempTfClient)
