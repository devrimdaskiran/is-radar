@echo off
REM AI Career Radar - Windows kurulum (bir kez calistir)
cd /d "%~dp0.."
echo === AI Career Radar kurulumu ===
py -3 --version >nul 2>&1
if errorlevel 1 (
  echo [HATA] Python bulunamadi. https://www.python.org/downloads/ adresinden Python 3.12 kur,
  echo kurulumda "Add python.exe to PATH" kutusunu isaretle, sonra bu dosyayi tekrar calistir.
  pause
  exit /b 1
)
if not exist .venv (
  echo Sanal ortam olusturuluyor...
  py -3 -m venv .venv
)
echo Paketler kuruluyor (birkac dakika surebilir)...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [HATA] Paket kurulumu basarisiz. Internet baglantini kontrol et.
  pause
  exit /b 1
)
if not exist .env (
  copy .env.example .env >nul
  echo .env dosyasi olusturuldu. Simdi Not Defteri'nde acilacak: degerleri doldurup kaydet.
  notepad .env
)
echo Testler calistiriliyor...
.venv\Scripts\python -m pytest -q
echo.
echo Kurulum bitti. Sonraki adimlar:
echo   1) scripts\telegram_id_ogren.bat  (kullanici ID'ni .env'ye yazmak icin)
echo   2) scripts\kontrol.bat            (ayarlari kontrol et)
echo   3) scripts\baslat.bat             (botu baslat)
echo   4) scripts\otomatik_baslat_ekle.bat (Windows acilinca otomatik baslasin)
pause
