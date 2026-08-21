@echo off
rem One-click dev launcher (backend + frontend): double-click, or run from cmd/PowerShell.
rem   start.cmd --backend-only     backend only
rem   start.cmd --port 8899        change the backend port
rem All logic lives in scripts\dev.py (documented in Chinese there); this file only
rem locates a Python and forwards the arguments.
rem NOTE: ASCII only on purpose - cmd.exe parses .cmd files using the console codepage,
rem so UTF-8 Chinese comments here would corrupt the parser (see scripts\dev.py).
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%backend\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%ROOT%scripts\dev.py" %*
set "CODE=%ERRORLEVEL%"
rem Keep a double-clicked window open on failure so the suggestions stay readable.
if not "%CODE%"=="0" pause
exit /b %CODE%
