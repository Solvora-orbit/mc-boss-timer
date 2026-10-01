@echo off
rem 一键打包脚本：把整个程序打成 dist\MCBossTimer.exe（单文件，免安装）
cd /d %~dp0
if not exist .venv\Scripts\pyinstaller.exe (
    echo [错误] 未找到虚拟环境，请先运行: python -m venv .venv 并安装 requirements.txt
    pause
    exit /b 1
)
.venv\Scripts\pyinstaller --onefile --noconfirm --clean ^
    --name MCBossTimer ^
    --add-data "static;static" ^
    run.py
if errorlevel 1 (
    echo [错误] 打包失败
    pause
    exit /b 1
)
echo.
echo [完成] 输出文件: %~dp0dist\MCBossTimer.exe
pause
