#!/bin/bash
# 快速体检（监控用）：一行状态 + 异常检测。
cd /app/working/intern-register
NOW=$(TZ=Asia/Shanghai date '+%H:%M:%S')

# ① 核心指标（走看板缓存 API，毫秒级）
API=$(curl -s -b /tmp/ir_cookie.txt -m 5 http://localhost:8000/api/summary 2>/dev/null)
if [ -n "$API" ]; then
  read T_REG T_LOGIN T_KEY T_FAIL POOL ROUNDS STATUS < <(echo "$API" | .venv/bin/python -c "
import json,sys
d=json.load(sys.stdin)
f=d.get('funnel') or {}
print(f.get('reg'), f.get('login'), f.get('key'), d.get('today_fail'),
      d['loop'].get('pool'), d['loop'].get('rounds'), d['loop'].get('status'))" 2>/dev/null)
  KTOTAL=$(echo "$API" | .venv/bin/python -c "import json,sys; print(json.load(sys.stdin).get('total_keys'))" 2>/dev/null)
else
  T_REG="?"; KTOTAL="API失败"
fi

# ② 僵尸检测：run.py 年龄（>25min = 异常，-k 30 上限兜着）
RP=$(pgrep -f "u run\.py" | head -1)
if [ -n "$RP" ]; then
  AGE=$(ps -o etimes= -p $RP 2>/dev/null | tr -d ' ')
  AGEM=$(( ${AGE:-0} / 60 ))
  AGE_S="run.py ${AGEM}m"
else
  AGE_S="run.py无(轮间隙)"
  AGEM=0
fi

# ③ 资源
MEM=$(free -m | awk 'NR==2{print $7}')
CB=$(pgrep -f camoufox | wc -l)

# ④ 熔断标记 + 日志末轮
FLAG=""; [ -f ledger/.login_blocked ] && FLAG="[熔断ON]"
LASTROUND=$(grep -a "ROUND" logs/loop.log | tail -1 | grep -oE "ROUND [0-9]+ +[0-9: -]+" | head -c 30)

# ⑤ 判定
WARN=""
[ "$AGEM" -gt 25 ] 2>/dev/null && WARN="$WARN ⚠run.py超龄${AGEM}m"
[ "${STATUS:-RUNNING}" != "RUNNING" ] && WARN="$WARN ⚠regloop=$STATUS"
[ "${MEM:-9999}" -lt 400 ] 2>/dev/null && WARN="$WARN ⚠内存${MEM}M"
[ "${CB:-0}" -gt 8 ] 2>/dev/null && WARN="$WARN ⚠浏览器${CB}个"
echo "$NOW | 累计key=$KTOTAL 今日:注册${T_REG}/登录${T_LOGIN}/key${T_KEY}/败${T_FAIL} | 出口$POOL | $AGE_S | 浏览器${CB} 内存${MEM}M | $LASTROUND $FLAG $WARN"
