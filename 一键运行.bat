@echo off
setlocal
set "PYTHONIOENCODING=gbk:replace"

REM ============================================================
REM  Xiamen job crawler + matcher  |  one-click run
REM  Double-click this file: crawl -> filter -> export Excel
REM
REM  NOTE: keep this file in GBK + CRLF encoding.
REM ============================================================

set "PROJ=%~dp0"

REM ---- Optional local override (git-ignored) ----
REM  If .\local.bat exists it may pre-set VENV_PY for this machine.
REM  Template: local.bat.example . This file is NOT version-controlled.
if exist "%PROJ%local.bat" call "%PROJ%local.bat"
if defined VENV_PY goto :py_ready

REM ---- Auto-detect: .\.venv -> .\venv -> %PYTHON% -> python on PATH ----
if exist "%PROJ%.venv\Scripts\python.exe" set "VENV_PY=%PROJ%.venv\Scripts\python.exe"
if defined VENV_PY goto :py_ready
if exist "%PROJ%venv\Scripts\python.exe" set "VENV_PY=%PROJ%venv\Scripts\python.exe"
if defined VENV_PY goto :py_ready
if defined PYTHON set "VENV_PY=%PYTHON%"
if defined VENV_PY goto :py_ready
set "VENV_PY=python"
:py_ready

cd /d "%PROJ%"

echo.
echo ============================================================
echo   厦门在招岗位爬取与匹配筛选系统
echo ============================================================
echo.
echo   解释器：%VENV_PY%
echo.

"%VENV_PY%" -c "import requests, bs4, lxml, pandas, openpyxl" 2>nul
if errorlevel 1 (
    echo [提示] 依赖不完整，正在自动安装...
    "%VENV_PY%" -m pip install -r requirements.txt
    echo.
)

"%VENV_PY%" main.py

echo.
echo ============================================================
echo   运行结束。导出文件位于 output\ 目录下。
echo ============================================================
echo.
pause
