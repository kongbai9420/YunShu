@echo off
rem Yunshu 4.0 local preview. Usage: preview.bat [branch or PR number]
rem Examples: preview.bat    preview.bat 5    preview.bat p0-scaffold
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0preview.ps1" %*
if errorlevel 1 pause
