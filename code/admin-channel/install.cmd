@echo off
REM Admin channel installer - must run as Administrator
REM This file is intentionally pure ASCII: cmd.exe reads .cmd with the system
REM codepage (GBK on zh-CN), so UTF-8 Chinese in a .cmd gets shredded.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause