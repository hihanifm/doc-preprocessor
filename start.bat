@echo off
setlocal

echo ============================================
echo  Document Formatter - Starting...
echo ============================================

:: Check .env exists
if not exist ".env" (
    echo.
    echo [ERROR] .env file not found.
    echo Copy .env.example to .env and fill in your settings:
    echo   copy .env.example .env
    echo.
    pause
    exit /b 1
)

:: Create venv if it doesn't exist
if not exist ".venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        echo Make sure Python is installed and on your PATH.
        pause
        exit /b 1
    )
)

:: Activate venv
call .venv\Scripts\activate.bat

:: Install / update dependencies
echo Installing dependencies...
pip install -q -r requirements.txt

echo.
echo ============================================
echo  Server running at http://localhost:5000
echo  Press Ctrl+C to stop
echo ============================================
echo.

python app.py

pause
