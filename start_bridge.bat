@echo off
setlocal
title Telegram Claude Bridge v3.1

cd /d "%~dp0" || goto :end

if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" if not "%%A:~0,1%"=="#" set "%%A=%%B"
    )
)

echo ========================================
echo   Telegram Claude Bridge v3.1
echo   Persistent Claude Agent SDK
echo ========================================
echo.

if "%TELEGRAM_BOT_TOKEN%"=="" (
    echo [Error] TELEGRAM_BOT_TOKEN is not set.
    echo Copy .env.example to .env and fill in your values.
    goto :end
)

where claude >nul 2>nul
if errorlevel 1 (
    echo [Warn] claude command is not in PATH. Set CLAUDE_CLI_PATH if needed.
)

python -m pip install -r requirements.txt
if errorlevel 1 goto :end

python telegram_bridge_claude.py

:end
echo.
pause
endlocal
