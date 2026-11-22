@echo off
REM AI Career Radar - botu ve otomatik taramayi baslatir. Cokerse 30 sn sonra yeniden baslar.
cd /d "%~dp0.."
title AI Career Radar
:dongu
.venv\Scripts\python -m radar run
echo Radar durdu (kod %errorlevel%). 30 saniye sonra yeniden baslatiliyor... Kapatmak icin bu pencereyi kapat.
timeout /t 30 /nobreak >nul
goto dongu
