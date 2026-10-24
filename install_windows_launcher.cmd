@echo off
setlocal
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0install_windows_launcher.ps1" %*
exit /b %ERRORLEVEL%
