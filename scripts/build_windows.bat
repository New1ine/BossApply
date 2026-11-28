@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "NoDefaultCurrentDirectoryInExePath=1"
set "REQUIRE_UI_PROBE=0"
if /I "%~1"=="--release" (
    set "REQUIRE_UI_PROBE=1"
) else if not "%~1"=="" (
    >&2 echo Usage: build_windows.bat [--release]
    exit /b 1
)
if not "%~2"=="" (
    >&2 echo Usage: build_windows.bat [--release]
    exit /b 1
)
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

rem Keep Conda's Tcl/Tk DLLs and their scripts in the same Python environment.
for /f "delims=" %%P in ('python.exe -I -c "import sys; print(sys.prefix)"') do set "BUILD_PYTHON_PREFIX=%%P"
if exist "%BUILD_PYTHON_PREFIX%\Library\bin\." set "PATH=%BUILD_PYTHON_PREFIX%\Library\bin;%PATH%"

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

set "UI_PROBE="
if exist "build\android\BossApplyUiProbe.apk" set "UI_PROBE=--add-data "build\android\BossApplyUiProbe.apk:android-probe""
if "%REQUIRE_UI_PROBE%"=="1" if not defined UI_PROBE (
    >&2 echo Missing build\android\BossApplyUiProbe.apk. Build the phone component before creating a complete release.
    exit /b 1
)

if not exist "build\android\BossApplyScrollGesture.jar" (
    >&2 echo Missing build\android\BossApplyScrollGesture.jar. Build the uniform scroll gesture tool before packaging.
    exit /b 1
)
if not exist "build\scroll-gesture-bundle\." mkdir "build\scroll-gesture-bundle"
copy /Y "build\android\BossApplyScrollGesture.jar" "build\scroll-gesture-bundle\ScrollGesture.jar" >nul
if errorlevel 1 exit /b 1

set "STAGING_DIR=build\release-windows"
python.exe -I -m PyInstaller --noconfirm --clean --noupx --onefile --windowed --name BossApply ^
    --distpath "%STAGING_DIR%" ^
    --collect-data customtkinter ^
    --add-binary "%ADB_SOURCE%\adb.exe:platform-tools" ^
    --add-binary "%ADB_SOURCE%\AdbWinApi.dll:platform-tools" ^
    --add-binary "%ADB_SOURCE%\AdbWinUsbApi.dll:platform-tools" ^
    --add-data "build\scroll-gesture-bundle\ScrollGesture.jar:android-probe" ^
    %ADB_NOTICE% %UI_PROBE% gui.py
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
