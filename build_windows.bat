@echo off
setlocal
cd /d "%~dp0"

if /I not "%CONDA_DEFAULT_ENV%"=="bossapply" (
    echo Activate the bossapply Conda environment before building.
    exit /b 1
)
if not exist "%CONDA_PREFIX%\python.exe" (
    echo Python was not found in the bossapply Conda environment.
    exit /b 1
)
set "PYTHON_EXE=%CONDA_PREFIX%\python.exe"
set "PATH=%CONDA_PREFIX%;%CONDA_PREFIX%\Library\bin;%CONDA_PREFIX%\Scripts;%PATH%"
echo Building with %PYTHON_EXE%

set "ADB_SOURCE=platform-tools"
if not exist "%ADB_SOURCE%\adb.exe" set "ADB_SOURCE=platform-tools\platform-tools-windows"
if not exist "%ADB_SOURCE%\adb.exe" (
    echo ADB was not found under platform-tools. Add Android SDK Platform-Tools before building.
    exit /b 1
)
if not exist "%ADB_SOURCE%\AdbWinApi.dll" (
    echo Missing AdbWinApi.dll in %ADB_SOURCE%
    exit /b 1
)
if not exist "%ADB_SOURCE%\AdbWinUsbApi.dll" (
    echo Missing AdbWinUsbApi.dll in %ADB_SOURCE%
    exit /b 1
)
set "ADB_OPTIONS=--add-binary "%ADB_SOURCE%\adb.exe;platform-tools" --add-binary "%ADB_SOURCE%\AdbWinApi.dll;platform-tools" --add-binary "%ADB_SOURCE%\AdbWinUsbApi.dll;platform-tools""
if exist "%ADB_SOURCE%\NOTICE.txt" set "ADB_NOTICE=--add-data "%ADB_SOURCE%\NOTICE.txt;platform-tools""

"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean --onefile --windowed --name BossApply --collect-data customtkinter %ADB_OPTIONS% %ADB_NOTICE% gui.py
if errorlevel 1 exit /b 1
echo Build finished: dist\BossApply.exe
