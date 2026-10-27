@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0browsers\ms-playwright"
set "PYTHONUTF8=1"
start "" "%~dp0python\pythonw.exe" "%~dp0edu\jtzy_auto_learn.pyw" %*
