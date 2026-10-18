@echo off
REM 打包为单文件 exe，产物在 dist\MergeSearch.exe
if not exist .venv\Scripts\python.exe (
    echo 请先创建虚拟环境: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt pyinstaller typer
    exit /b 1
)
REM Anaconda Python 需手动带上 conda 的 DLL（python.org 版 Python 可删掉这些 --add-binary）
set CONDA_BIN=D:/software/anaconda3/Library/bin
.venv\Scripts\python.exe -m PyInstaller --onefile --windowed --name MergeSearch ^
    --copy-metadata mcp --collect-submodules mcp --collect-submodules uvicorn ^
    --collect-all ttkbootstrap --collect-all webview ^
    --add-data "web;web" --add-data "assets;assets" ^
    --icon assets/icon.ico ^
    --add-binary "%CONDA_BIN%/ffi.dll;." ^
    --add-binary "%CONDA_BIN%/libbz2.dll;." ^
    --add-binary "%CONDA_BIN%/tcl86t.dll;." ^
    --add-binary "%CONDA_BIN%/tk86t.dll;." ^
    --add-binary "%CONDA_BIN%/zlib.dll;." ^
    --add-binary "%CONDA_BIN%/libssl-3-x64.dll;." ^
    --add-binary "%CONDA_BIN%/libcrypto-3-x64.dll;." ^
    --clean --noconfirm main.py
echo.
echo 完成: dist\MergeSearch.exe
