@echo off
chcp 65001 <nul >nul
:: chcp reads stdin when it is redirected, so give it nul (it ate a piped answer in the 2026-10-15 test)
cd /d "%~dp0"
:: Keep this file ASCII only (same reason as the main launcher). The version control menu (2026-10-21) is
:: tools\git_menu.py; why it works the way it does is at the top of that file and in docs section 4.8.
if exist ".venv\Scripts\python.exe" goto run
type "tools\messages\no_venv.txt"
pause
exit /b 1
:run
".venv\Scripts\python.exe" tools\git_menu.py %*
if errorlevel 1 pause
exit /b 0
