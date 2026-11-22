@echo off
REM Windows'a giris yapildiginda radar'i simge durumunda baslatir.
cd /d "%~dp0.."
schtasks /Create /SC ONLOGON /TN "AI Career Radar" /TR "cmd /c start \"AI Career Radar\" /min \"%CD%\scripts\baslat.bat\"" /RL LIMITED /F
if errorlevel 1 (
  echo [HATA] Gorev olusturulamadi.
) else (
  echo Tamam: Windows'a her giriste AI Career Radar otomatik baslayacak.
  echo Kaldirmak icin: schtasks /Delete /TN "AI Career Radar" /F
)
pause
