@echo off
title Telegram Claude Bridge v2.6
echo ========================================
echo   Telegram - Claude Code Bridge v2.6
echo   (Modular: vision + url_fetchers)
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

REM Check google-generativeai (optional)
python -c "import google.generativeai" >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [Note] google-generativeai not installed. Image analysis will be disabled.
    echo To enable: pip install google-generativeai
)

echo.
echo [Starting] Telegram Claude Bridge v2.6...
echo.

python telegram_bridge_claude.py

pause
