@echo off
REM NATIVE (Qt) build of MarioKartNitro.exe - no web engine for the launcher itself.
REM (The Mii editor still opens its own small web window on demand.)
REM Builds MarioKartNitro.exe — run this ON WINDOWS, inside this folder.
REM Requires Python 3.10+ installed and on PATH.

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install PySide6-Essentials
if exist __pycache__ rmdir /s /q __pycache__

echo Building standalone EXE with the Windows Common Controls manifest...
python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx ^
  --name "MarioKartNitro" ^
  --icon "build_assets\icon.ico" ^
  --manifest "MarioKartNitro.manifest" ^
  --version-file "version_info.txt" ^
  --splash "build_assets\splash.png" ^
  --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module tkinter --exclude-module numpy ^
  --exclude-module PySide6.QtNetwork --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtPdf --exclude-module PySide6.QtOpenGL ^
  --add-data "index.html;." ^
  --add-data "logo.png;." ^
  --add-data "banner.png;." ^
  --add-data "mii_renderer;mii_renderer" ^
  --add-data "fonts;fonts" ^
  --add-data "starter.mii;." ^
  --add-data "RFL_Res.dat;." ^
  --add-data "rcedit.exe;." ^
  main.py
if errorlevel 1 goto failed
copy /Y "MarioKartNitro.manifest" "dist\MarioKartNitro.exe.manifest" >nul
echo.
echo Build complete: dist\MarioKartNitro.exe
echo Nitro Miis are saved locally and sync into the selected Dolphin NAND on Play.
pause
goto end
:failed
echo.
echo BUILD FAILED. Read the error above; no EXE was created.
pause
:end
