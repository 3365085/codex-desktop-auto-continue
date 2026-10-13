@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1" %*
pause
