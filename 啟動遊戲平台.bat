@echo off
chcp 65001 <nul >nul
:: chcp reads stdin when it is redirected, so give it nul (it ate a piped answer in the 2026-10-15 test)
cd /d "%~dp0"
:: Keep this file ASCII only: cmd misreads a UTF-8 .bat that contains Chinese and runs half-lines as commands
:: (found 2026-10-15, see docs section 7). Chinese messages are in tools\messages\*.txt, and everything after
:: Python is found (packages, database, game engines, Ollama check, server window, browser) is in tools\launch.py.
if exist ".venv\Scripts\python.exe" goto run
:: Find a Python that Django 5.2 supports (3.10-3.13) through the py launcher. Never the bare python command:
:: it may be the Microsoft Store stub or another program's virtual environment.
set "PY="
for %%v in (3.11 3.12 3.13 3.10) do if not defined PY py -%%v -c "" >nul 2>&1 && set "PY=py -%%v"
if defined PY goto make_venv
type "tools\messages\no_python.txt"
where winget >nul 2>&1 || goto python_manual
choice /c YN
if errorlevel 2 goto python_manual
winget install -e --id Python.Python.3.11 --scope user || goto python_manual
:: The new Python is not on this window's PATH yet, so use its install location
set PY="%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist %PY% goto python_manual
:make_venv
type "tools\messages\creating_venv.txt"
%PY% -m venv .venv || goto failed
:run
".venv\Scripts\python.exe" tools\launch.py %*
if errorlevel 1 goto failed
exit /b 0

:python_manual
type "tools\messages\python_manual.txt"
:failed
pause
exit /b 1
