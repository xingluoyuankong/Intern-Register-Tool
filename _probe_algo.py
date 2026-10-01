"""受控实验：纯 python 算 acw_sc__v2（不再用浏览器）+ 同 session 重放。

背景：Resin 网关是**每请求轮换出口**（实测同用户名 4 发 3 个不同 IP），
"首发 requests → 浏览器算 cookie → requests 重放"三步走三个出口，
昨晚 10/10 成功是运气（当时出口会话稳定），今天全灭。
本实验把 cookie 计算收进 requests 同一个 session（连接复用 ⇒ 出口一致概率最高），
若成功则整个 WAF solver 不再需要浏览器。

acw_sc__v2 标准算法（阿里云 WAF 初级 JS 挑战，公开逆向结论）：
  arg1（服务端 40 位 hex）与固定 mask '3000176000856006061501533003690027800375'
  按字节 hex 异或，再按位置表 posList 重排，得 40 位 hex cookie。
"""
import sys
import time

sys.path.insert(0, ".")
import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402

POS_LIST = [15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25,
            13, 6, 11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17, 5, 3,
            28, 34, 37, 12, 36]
MASK = "3000176000856006061501533003690027800375"


def _xor_hex(mask: str, arg: str) -> str:
    out = ""
    for i in range(0, min(len(mask), len(arg)), 2):
        a = int(mask[i:i + 2], 16)
        b = int(arg[i:i + 2], 16)
        out += f"{a ^ b:02x}"
    return out


def acw_sc_v2(arg1: str) -> str:
    xored = list(_xor_hex(MASK, arg1))
    res = [""] * 40
    for i, pos in enumerate(POS_LIST):
        if i < len(xored):
            res[pos - 1] = xored[i]
    return "".join(res)


def main() -> None:
    sso = SSOClient()
    url = f"{config.SSO_GW}/register/byEmail"
    h = sso._headers("/register")
    tag = str(int(time.time()))[-8:]
    email = f"algo{tag}@example.invalid"
    payload = {"username": f"al{tag[-6:]}", "email": email,
               "password": encrypt_password(email, "Watch!2026x"),
               "source": config.SOURCE, "clientId": config.CLIENT_ID}

    r1 = sso.session.post(url, headers=h, json=payload, timeout=30)
    is_ch = "acw_sc__v2" in r1.text
    print("[1] first:", r1.status_code, "challenge" if is_ch else "json",
          flush=True)
    if not is_ch:
        print("body:", r1.text[:200], flush=True)
        return

    import re as _re
    m = _re.search(r"arg1='([0-9A-Fa-f]{40})'", r1.text)
    if not m:
        print("arg1 提取失败（可能算法升级）", flush=True)
        return
    arg1 = m.group(1)
    cookie_val = acw_sc_v2(arg1)
    print("[2] arg1:", arg1, "-> cookie:", cookie_val[:24] + "...", flush=True)

    sso.session.cookies.set("acw_sc__v2", cookie_val,
                            domain="sso.openxlab.org.cn", path="/")
    r2 = sso.session.post(url, headers=h, json=payload, timeout=30)
    ch2 = "acw_sc__v2" in r2.text
    print("[3] replay:", r2.status_code,
          "challenge" if ch2 else "json", "| body:", r2.text[:200], flush=True)


if __name__ == "__main__":
    main()
