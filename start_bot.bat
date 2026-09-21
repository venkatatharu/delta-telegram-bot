@echo off
REM ============================================================
REM  Delta Exchange — Telegram Trading Bot  (one-click launcher)
REM  Double-click to start. Press Ctrl+C (or close the window) to stop.
REM  The bot reads its secrets from the .env file in THIS folder.
REM ============================================================
setlocal
cd /d "%~dp0"

REM --- Create the virtual environment on first run ---
if not exist ".venv\Scripts\activate.bat" (
    echo [setup] Creating Python virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo [error] Could not create venv. Is Python 3 installed and on PATH?
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

REM --- Install / refresh dependencies (quiet) ---
echo [setup] Installing dependencies...
python -m pip install -r requirements.txt -q

echo.
echo  ============================================================
echo   Starting Delta Telegram Trading Bot  ^(testnet unless .env says otherwise^)
echo   Open Telegram and send your bot: /start
echo   Press Ctrl+C in this window to stop.
echo  ============================================================
echo.

python -X utf8 delta_telegram_bot.py

echo.
echo  Bot stopped.
pause
endlocal
