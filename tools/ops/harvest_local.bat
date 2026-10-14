@echo off
rem 本地收割 wrapper：切到项目根再跑 harvest_local.py（自身写 logs/harvest_local.log）。
rem 用 %~dp0 定位，避免在脚本里写中文绝对路径。
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -u tools\ops\harvest_local.py
