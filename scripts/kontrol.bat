@echo off
cd /d "%~dp0.."
.venv\Scripts\python -m radar check
echo.
echo Tek seferlik tarama yapiliyor (bildirim gondermez)...
.venv\Scripts\python -m radar scan
pause
