@echo off
chcp 65001 <nul >nul
:: chcp reads stdin when it is redirected, so give it nul (it ate a piped answer in the 2026-10-15 test)
cd /d "%~dp0"
:: Keep this file ASCII only (same reason as the main launcher). Local test mode, user decision 2026-10-15:
:: tools\start_local_test.py uses local Ollama for this run only; why it works the way it does is at the top of that file.
if exist ".venv\Scripts\python.exe" goto run
type "tools\messages\no_venv.txt"
pause
exit /b 1
:run
".venv\Scripts\python.exe" manage.py migrate --noinput >nul || goto failed
".venv\Scripts\python.exe" tools\start_local_test.py %*
if errorlevel 1 goto failed
exit /b 0

:failed
pause
exit /b 1
