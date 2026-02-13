@echo off
title Telegram Claude Bridge v2.4
echo ========================================
echo   Telegram Claude Code Bridge v2.4
echo   (Context Memory + URL Fetch + LangExtract)
echo   Press Ctrl+C to stop
echo ========================================
echo.

cd /d "%~dp0"

REM Check Claude CLI
where claude >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [Error] Claude CLI not found!
    echo Please install: npm install -g @anthropic-ai/claude-code
    pause
    exit /b 1
)

REM Check .env file
if not exist ".env" (
    echo [Error] .env file not found!
    echo Please copy .env.example to .env and configure it.
    pause
    exit /b 1
)

REM Check Python dependencies
python -c "import telegram" >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [Installing] python-telegram-bot...
    pip install -r requirements.txt
)

echo.
echo [Starting] Telegram Claude Bridge v2.4...
echo.

python telegram_claude_bridge.py

pause
