@echo off
REM Windows launcher.
REM Keep variables local to this script and use its own directory as cwd.
setlocal
cd /d "%~dp0"
REM Prefer the project virtual environment; otherwise use Python from PATH.
set "PC_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PC_PYTHON=.venv\Scripts\python.exe"
REM Quietly check imports before opening the web server.
"%PC_PYTHON%" -c "import streamlit, langchain_openai, pydantic, dotenv, requests, PIL, pypdf" >nul 2>&1
if errorlevel 1 (
  REM A nonzero exit code means dependencies or Python are unavailable.
  echo Dependencies are missing. Please run install.bat first.
  pause
  exit /b 1
)
REM Listen only on this computer at port 8504; disable usage statistics.
"%PC_PYTHON%" -m streamlit run app.py --server.address 127.0.0.1 --server.port 8504 --browser.gatherUsageStats false
REM Keep the terminal visible after the server exits so errors can be read.
pause
