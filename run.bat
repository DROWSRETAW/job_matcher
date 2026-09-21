@echo off
setlocal
set "PYTHONIOENCODING=gbk:replace"

REM ============================================================
REM  Xiamen job crawler + matcher  |  generic entry point
REM
REM  Usage (append params directly):
REM    run.bat                      crawl + default filter + export
REM    run.bat xiamen               filter Xiamen only (no network)
REM    run.bat --skip-crawl --city 厦门 --min-score 16 --level S,A
REM    run.bat --stats              database overview
REM    run.bat -h                   list all options
REM    run.bat test                 run all unit tests
REM    run.bat spider               test the XMU crawler alone
REM    run.bat ods                  ODS layer + crawl batch ledger
REM    run.bat rebuild              rebuild state layer from ODS (offline)
REM
REM  NOTE: keep this file in GBK + CRLF encoding. Do not "fix" it to
REM  UTF-8; cmd.exe on zh-CN Windows will garble it. .gitattributes
REM  already pins CRLF for *.bat.
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

REM ---- Dependency self-check ----
"%VENV_PY%" -c "import requests, bs4, lxml, pandas, openpyxl" 2>nul
if errorlevel 1 (
    echo [提示] 当前解释器缺少依赖，正在按 requirements.txt 安装...
    echo        解释器：%VENV_PY%
    "%VENV_PY%" -m pip install -r requirements.txt
    echo.
)

if /i "%~1"=="xiamen" (
    echo [只筛厦门岗位 ^| 使用库中已有数据，不联网]
    "%VENV_PY%" main.py --skip-crawl --city 厦门
    echo.
    pause
    exit /b 0
)

if /i "%~1"=="test" (
    echo [运行全部单元测试]
    "%VENV_PY%" -m pytest tests/ -q
    echo.
    pause
    exit /b 0
)

if /i "%~1"=="spider" (
    echo [单独测试厦大就业网爬虫]
    "%VENV_PY%" spiders\xmu_career.py
    echo.
    pause
    exit /b 0
)

if /i "%~1"=="ods" (
    echo [ODS 贴源层概览 ^| 快照数量与历次抓取批次]
    "%VENV_PY%" main.py --ods-stats
    echo.
    pause
    exit /b 0
)

if /i "%~1"=="rebuild" (
    echo [从 ODS 原始快照重建最新状态层 ^| 不联网]
    "%VENV_PY%" main.py --rebuild-state
    echo.
    pause
    exit /b 0
)

echo.
echo ============================================================
echo   岗位爬取与匹配筛选  ^|  参数: %*
echo ============================================================
echo.

"%VENV_PY%" main.py %*

echo.
pause
