@echo off
REM 打包为单文件 exe，产物在 dist\MergeSearch.exe
if not exist .venv\Scripts\python.exe (
    echo 请先创建虚拟环境: python -m venv .venv ^&^ .venv\Scripts\pip install -r requirements.txt pyinstaller
    exit /b 1
)
.venv\Scripts\python.exe -m PyInstaller --onefile --windowed --name MergeSearch ^
    --copy-metadata mcp --collect-submodules mcp --collect-submodules uvicorn ^
    --collect-all ttkbootstrap --collect-all webview ^
    --add-data "web;web" --add-data "assets;assets" ^
    --icon assets/icon.ico ^
    --clean --noconfirm main.py
echo.
echo 完成: dist\MergeSearch.exe
