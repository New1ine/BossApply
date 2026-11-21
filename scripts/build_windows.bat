@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "NoDefaultCurrentDirectoryInExePath=1"
pushd "%~dp0.." || exit /b 1

call :build
if errorlevel 1 (
    popd
    exit /b 1
)
popd
exit /b 0

:build
python.exe -I -c "from importlib.metadata import version; import qrcode; from PIL import ImageTk; version('PyInstaller'); version('customtkinter')"
if errorlevel 1 (
    >&2 echo Activate the Python environment with the project requirements and PyInstaller installed.
    exit /b 1
)

set "ADB_SOURCE=platform-tools"
if not exist "%ADB_SOURCE%\adb.exe" set "ADB_SOURCE=platform-tools\platform-tools-windows"
for %%F in (adb.exe AdbWinApi.dll AdbWinUsbApi.dll) do (
    if not exist "%ADB_SOURCE%\%%F" (
        >&2 echo Missing %ADB_SOURCE%\%%F. Install Android SDK Platform-Tools before building.
        exit /b 1
    )
)

set "ADB_NOTICE="
if exist "%ADB_SOURCE%\NOTICE.txt" set "ADB_NOTICE=--add-data "%ADB_SOURCE%\NOTICE.txt:platform-tools""

set "STAGING_DIR=build\release-windows"
python.exe -I -m PyInstaller --noconfirm --clean --noupx --onefile --windowed --name BossApply ^
    --distpath "%STAGING_DIR%" ^
    --collect-data customtkinter ^
    --add-binary "%ADB_SOURCE%\adb.exe:platform-tools" ^
    --add-binary "%ADB_SOURCE%\AdbWinApi.dll:platform-tools" ^
    --add-binary "%ADB_SOURCE%\AdbWinUsbApi.dll:platform-tools" ^
    %ADB_NOTICE% gui.py
if errorlevel 1 exit /b 1
if not exist "%STAGING_DIR%\BossApply.exe" (
    >&2 echo Build output %STAGING_DIR%\BossApply.exe is missing.
    exit /b 1
)
if not exist "dist\." mkdir "dist"
move /Y "%STAGING_DIR%\BossApply.exe" "dist\BossApply.exe" >nul
if errorlevel 1 exit /b 1
echo Build finished: dist\BossApply.exe
exit /b 0
