@echo off
setlocal
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0windows_launcher.ps1" %*
exit /b %ERRORLEVEL%
