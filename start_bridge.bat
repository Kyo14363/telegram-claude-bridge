@echo off
setlocal
title Telegram Claude Bridge v3.3 (Persistent SDK)

cd /d "%~dp0" || goto :end

REM === Load .env ===
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" if not "%%A:~0,1%"=="#" set "%%A=%%B"
    )
)

echo ========================================
echo   Telegram Claude Bridge v3.3
echo   Persistent Claude Agent SDK
echo ========================================
echo.

if "%TELEGRAM_BOT_TOKEN%"=="" (
    echo [Error] TELEGRAM_BOT_TOKEN is not set.
    echo Copy .env.example to .env and fill in your values.
    goto :end
)

REM === Dependency checks ===
where claude >nul 2>nul
if errorlevel 1 (
    echo [Warn] claude command is not in PATH. Set CLAUDE_CLI_PATH if needed.
)

python -m pip install -r requirements.txt
if errorlevel 1 goto :end

REM === Run ===
python telegram_bridge_claude.py
echo.
echo [Bridge exited with code %ERRORLEVEL%]

:end
echo.
pause
endlocal
