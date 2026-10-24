@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1" %*
exit /b %ERRORLEVEL%
