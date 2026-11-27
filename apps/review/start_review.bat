@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
    echo Usage: start_review.bat ^<recognized_dir^>
    pause
    exit /b 1
)
python server.py --dir "%~1" --port 8904 --lan
pause
