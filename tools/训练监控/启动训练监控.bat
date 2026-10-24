@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py3136 >nul 2>nul
if %errorlevel%==0 (
  call py3136 serve.py
) else (
  "D:\Dev\Python\3.13.6\python.exe" serve.py
)
echo.
echo 监控已停止。按任意键关闭窗口。
pause >nul
