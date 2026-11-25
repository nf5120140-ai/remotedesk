@echo off
chcp 65001 >nul
REM Builds dist\RemoteDesk.exe and (if Inno Setup is installed) dist\RemoteDesk-Setup.exe
cd /d %~dp0
python -m pip install -r requirements.txt pyinstaller || goto :err
python -m PyInstaller --noconfirm --onefile --windowed --name RemoteDesk --icon icon.ico ^
  --hidden-import win32timezone --hidden-import servicemanager ^
  --hidden-import win32serviceutil --hidden-import win32ts --hidden-import win32profile ^
  --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
  app.py || goto :err
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  %ISCC% installer\RemoteDesk.iss || goto :err
  echo.
  echo  Created: dist\RemoteDesk-Setup.exe
) else (
  echo.
  echo  Created: dist\RemoteDesk.exe
  echo  For an installer, install Inno Setup 6 from jrsoftware.org and run again.
)
pause
exit /b 0
:err
echo BUILD FAILED
pause
exit /b 1
