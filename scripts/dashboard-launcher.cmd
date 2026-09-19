@echo off
setlocal EnableExtensions

for %%I in ("%~dp0..") do set "REPO=%%~fI"

rem Use the existing supported launcher and dashboard command. The CLI opens
rem its loopback URL after binding a free local port.
powershell.exe -NoProfile -File "%REPO%\scripts\dual-codex.ps1" --config "%REPO%\config.toml" dashboard
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo Dual Agents Dashboard failed to start (exit code %EXITCODE%).
    pause
)

exit /b %EXITCODE%
