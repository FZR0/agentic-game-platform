@echo off
chcp 65001 <nul >nul
:: chcp reads stdin when it is redirected, so give it nul (it ate a piped answer in the 2026-10-15 test)
cd /d "%~dp0"
:: Keep this file ASCII only (same reason as the main launcher). PvP test over a Cloudflare quick tunnel,
:: user decision 2026-11-05: tools\start_tunnel.py downloads cloudflared once, runs the site in public mode and prints the URL.
if exist ".venv\Scripts\python.exe" goto run
type "tools\messages\no_venv.txt"
pause
exit /b 1
:run
".venv\Scripts\python.exe" manage.py migrate --noinput >nul || goto failed
".venv\Scripts\python.exe" tools\start_tunnel.py %*
if errorlevel 1 goto failed
exit /b 0

:failed
pause
exit /b 1
