#!/bin/bash
# 服务器：装 JS 引擎并自检
echo "== node =="
which node nodejs 2>/dev/null || echo "no system node"
ls /root/.cache/ms-playwright/*/node*/node 2>/dev/null | head -2

cd /root/intern-register || exit 1
echo "== try py-mini-racer =="
.venv/bin/pip install -q py-mini-racer -i https://pypi.tuna.tsinghua.edu.cn/simple 2>&1 | tail -2
.venv/bin/python - <<'PY'
try:
    from py_mini_racer import py_mini_racer
    c = py_mini_racer.MiniRacer()
    print("py-mini-racer ok:", c.eval("1+1"))
except Exception as ex:
    print("py-mini-racer FAIL:", type(ex).__name__, str(ex)[:120])
PY
