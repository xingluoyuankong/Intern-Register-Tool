@echo off
chcp 65001 >nul
REM Intern-Register 台账「服务器 → 本地」自动同步（防丢失）
REM 由 WorkBuddy 自动化 / 手动双击均可触发。
cd /d "E:\API获取工具\Intern-Register"
if not exist "logs" mkdir "logs"
echo. >> "logs\sync_auto.log"
echo ===== %date% %time% ===== >> "logs\sync_auto.log"
".venv\Scripts\python.exe" -u tools\ops\sync_server_ledger.py >> "logs\sync_auto.log" 2>&1
echo exit=%ERRORLEVEL% >> "logs\sync_auto.log"
