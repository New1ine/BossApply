#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."

require_probe=0
if [[ $# -eq 1 && "${1-}" == --release ]]; then
    require_probe=1
elif [[ $# -ne 0 ]]; then
    printf '%s\n' 'Usage: build_linux.sh [--release]' >&2
    exit 1
fi

if ! python -I -c "from importlib.metadata import version; import qrcode; from PIL import ImageTk; version('PyInstaller'); version('customtkinter')"; then
    printf '%s\n' 'Activate the Python environment with the project requirements and PyInstaller installed.' >&2
    exit 1
fi

probe_args=()
if [[ -f build/android/BossApplyUiProbe.apk ]]; then
    probe_args=(--add-data 'build/android/BossApplyUiProbe.apk:android-probe')
elif [[ "$require_probe" -eq 1 ]]; then
    printf '%s\n' 'Missing build/android/BossApplyUiProbe.apk. Build or copy the phone component before creating a complete release.' >&2
    exit 1
fi

if [[ ! -f build/android/BossApplyScrollGesture.jar ]]; then
    printf '%s\n' 'Missing build/android/BossApplyScrollGesture.jar. Build or copy the uniform scroll gesture tool before packaging.' >&2
    exit 1
fi
install -Dm644 build/android/BossApplyScrollGesture.jar build/scroll-gesture-bundle/ScrollGesture.jar

python -I -m PyInstaller --noconfirm --clean --noupx --onedir --name BossApply \
    --collect-data customtkinter --add-data 'build/scroll-gesture-bundle/ScrollGesture.jar:android-probe' \
    "${probe_args[@]}" gui.py

output="dist/BossApply"
if [[ ! -x "$output/BossApply" ]]; then
    printf 'Build output %s/BossApply is missing or not executable.\n' "$output" >&2
    exit 1
fi

adb_source="platform-tools"
if [[ ! -f "$adb_source/adb" ]]; then
    adb_source="platform-tools/platform-tools-linux"
fi
if [[ -f "$adb_source/adb" ]]; then
    install -Dm755 "$adb_source/adb" "$output/platform-tools/adb"
    if [[ -f "$adb_source/NOTICE.txt" ]]; then
        install -m644 "$adb_source/NOTICE.txt" "$output/platform-tools/NOTICE.txt"
    fi
else
    printf '%s\n' 'ADB was not bundled. Install adb on the target Linux system.'
fi
printf 'Build finished: %s/BossApply\n' "$output"
