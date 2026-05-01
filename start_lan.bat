@echo off
setlocal

echo ============================================
echo  Doc Clinic - LAN Mode
echo  (accessible from other machines)
echo ============================================

if not exist ".env" (
    echo.
    echo [ERROR] .env file not found.
    echo Copy .env.example to .env and fill in your settings.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

:: Print local IP so user knows where to point other machines
echo.
echo Your LAN IP address(es):
ipconfig | findstr /i "IPv4"
echo.
echo ============================================
echo  Server running at http://0.0.0.0:5000
echo  Other devices: use the IP above with :5000
echo  Press Ctrl+C to stop
echo ============================================
echo.

python app.py --host 0.0.0.0

pause
