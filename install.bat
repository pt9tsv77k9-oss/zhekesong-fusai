@echo off
REM Windows installer.
REM Work inside this script's directory; variables stay local.
setlocal
cd /d "%~dp0"
REM Create an isolated Python environment in the .venv folder.
python -m venv .venv
if errorlevel 1 (
  REM Stop if Python is missing or virtual environment creation failed.
  echo Install Python 3.11 or newer and enable Add Python to PATH.
  pause
  exit /b 1
)
REM Install the packages and exact versions listed in requirements.txt.
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  REM Do not report success when dependency installation failed.
  echo Installation failed. Check your network, then run install.bat again.
  pause
  exit /b 1
)
if not exist ".env" copy /y ".env.example" ".env" >nul
REM Leave the result visible until a key is pressed.
echo Setup complete. Edit .env, then double-click start.bat.
pause
