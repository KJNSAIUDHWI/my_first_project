@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动方向确认工具...
where py3136 >nul 2>nul
if %errorlevel%==0 (
    py3136 serve.py
) else (
    "D:\Dev\Python\3.13.6\python.exe" serve.py
)
echo.
echo 服务已停止。
pause
