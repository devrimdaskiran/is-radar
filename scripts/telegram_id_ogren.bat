@echo off
cd /d "%~dp0.."
echo Telegram'da kendi botuna herhangi bir mesaj gonder. ID'n asagida yazacak.
.venv\Scripts\python -m radar whoami
pause
