#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python -m PyInstaller --noconfirm --clean --onedir --name BossApply gui.py
adb_source="platform-tools"
if [[ ! -x "$adb_source/adb" ]]; then adb_source="platform-tools/platform-tools-linux"; fi
if [[ -x "$adb_source/adb" ]]; then
    mkdir -p "dist/BossApply/platform-tools"
    cp "$adb_source/adb" "dist/BossApply/platform-tools/adb"
    if [[ -f "$adb_source/NOTICE.txt" ]]; then cp "$adb_source/NOTICE.txt" "dist/BossApply/platform-tools/NOTICE.txt"; fi
fi
echo "Build finished: dist/BossApply/BossApply"
if [[ ! -x "$adb_source/adb" ]]; then echo "NOTE: adb not bundled. Install adb on this Linux system."; fi
