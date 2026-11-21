#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."

if ! python -I -c "from importlib.metadata import version; import qrcode; from PIL import ImageTk; version('PyInstaller'); version('customtkinter')"; then
    printf '%s\n' 'Activate the Python environment with the project requirements and PyInstaller installed.' >&2
    exit 1
fi

python -I -m PyInstaller --noconfirm --clean --noupx --onedir --name BossApply \
    --collect-data customtkinter gui.py

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
