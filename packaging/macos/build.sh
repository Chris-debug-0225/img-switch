#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
python="${PYTHON:-python3}"
output="${1:-$root/dist}"
arch="$(uname -m)"
case "$arch" in arm64|x86_64) ;; *) echo "Unsupported architecture: $arch" >&2; exit 1;; esac
"$python" -c 'import tkinter, PyInstaller, pycdlib, importlib.metadata as m; assert m.version("pycdlib") == "1.22.0", "Install pycdlib==1.22.0"'
mkdir -p "$output"
output="$(cd "$output" && pwd)"
dmg="$output/IMG-Switch-macOS-$arch-unsigned.dmg"
if [ -e "$dmg" ]; then echo "Refusing to overwrite: $dmg" >&2; exit 1; fi
work="$(mktemp -d /tmp/img-switch-build.XXXXXXXX)"
trap 'rm -rf "$work"' EXIT
"$python" "$root/packaging/macos/stage.py" "$root" "$work/source"
mkdir -p "$work/cli-dist" "$work/gui-dist" "$work/licenses" "$work/source/engine/bin"
cp "$work/source/LICENSE" "$work/licenses/IMG-SWITCH.txt"
cp "$root/packaging/macos/licenses/TCL.txt" "$work/licenses/"
cp "$root/packaging/macos/licenses/TK.txt" "$work/licenses/"
stdlib="$("$python" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
if [ ! -f "$stdlib/LICENSE.txt" ]; then echo 'Python LICENSE.txt missing; cannot prepare package.' >&2; exit 1; fi
cp "$stdlib/LICENSE.txt" "$work/licenses/PYTHON.txt"
pyinstaller_license="$("$python" -c 'import importlib.metadata as m; d=m.distribution("pyinstaller"); files=[f for f in d.files if str(f).endswith("/COPYING.txt")]; assert len(files)==1, "PyInstaller license missing"; print(d.locate_file(files[0]))')"
cp "$pyinstaller_license" "$work/licenses/PYINSTALLER.txt"
cd "$work/source"
"$python" -m PyInstaller --noconfirm --clean --onefile --console --name img-switch-engine --paths engine/scripts --distpath "$work/cli-dist" --workpath "$work/cli-build" --specpath "$work" engine/scripts/failover_imagegen_tool.py
cp "$work/cli-dist/img-switch-engine" engine/bin/img-switch-engine
"$python" -m PyInstaller --noconfirm --clean --onedir --windowed --name 'IMG Switch' --paths engine/scripts --hidden-import failovercfg --hidden-import urllib.request --hidden-import urllib.error --hidden-import urllib.parse --hidden-import glob --hidden-import uuid --hidden-import base64 --hidden-import argparse --hidden-import json --hidden-import time --hidden-import socket --add-data "$work/source/engine:engine" --add-data "$work/licenses:licenses" --distpath "$work/gui-dist" --workpath "$work/gui-build" --specpath "$work" app.py
mkdir -p "$work/dmg"
cp -R "$work/gui-dist/IMG Switch.app" "$work/dmg/"
ln -s /Applications "$work/dmg/Applications"
"$python" "$root/packaging/macos/make_dmg.py" "$work/dmg" "$work/image.iso"
hdiutil convert -quiet "$work/image.iso" -format UDZO -o "$work/final.dmg"
hdiutil convert -quiet "$work/final.dmg" -format UDTO -o "$work/roundtrip"
if ! cmp -s "$work/image.iso" "$work/roundtrip.cdr"; then
  echo 'DMG round-trip differs from the audited raw image.' >&2
  exit 1
fi
cp "$work/final.dmg" "$dmg"
shasum -a 256 "$dmg"
echo "Created unsigned (ad-hoc signed only) test DMG: $dmg"
