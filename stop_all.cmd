@echo off
setlocal
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0stop_all.ps1" %*
exit /b %ERRORLEVEL%
