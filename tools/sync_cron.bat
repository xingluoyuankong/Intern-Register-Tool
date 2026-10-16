@echo off
REM 服务器台账 -> 本地 自动同步（由 Windows 计划任务每 5 分钟调用）
REM 2026-10-14：常驻 sync_daemon.py 会被系统/会话中断，改用计划任务更可靠。
REM 🔴 2026-10-16：改为**相对定位**（%~dp0=本脚本目录），不再写死本机用户名/盘符。
cd /d "%~dp0.."
bash tools/sync_now.sh >> logs\sync_cron.log 2>&1
