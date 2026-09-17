#!/usr/bin/env bash
# Run the gate under GE-Proton's wine against a prefix umu prepared.
#   run-gate.sh <dir with nvngx_dlssnr.dll> <wineprefix> [W H N]
# The prefix must have been booted once by umu-run (it copies the driver's
# _nvngx.dll / nvngx.dll and dxvk-nvapi into system32):
#   WINEPREFIX=<prefix> PROTONPATH=GE-Proton11-5-x86_64 GAMEID=umu-neuralscreen umu-run wineboot -u
set -euo pipefail
dir="$1"; prefix="$2"; shift 2
GE="${GE:-$HOME/.local/share/Steam/compatibilitytools.d/GE-Proton11-5-x86_64/files}"
cp -n "$(dirname "${BASH_SOURCE[0]}")/nvngx.dll_gate.exe" "$dir/" 2>/dev/null || true
cd "$dir"
exec env WINEPREFIX="$prefix" PATH="$GE/bin:$PATH" WINEDLLPATH="$GE/lib/wine" \
    WINEDLLOVERRIDES="d3d12,d3d12core,dxgi,nvapi64,nvngx,_nvngx=n,b" \
    WINEESYNC=1 WINEFSYNC=1 DXVK_ENABLE_NVAPI=1 DXVK_NVAPI_GPU_ARCH="${DXVK_NVAPI_GPU_ARCH:-GB200}" \
    WINEDEBUG=-all "$GE/bin/wine" ./nvngx.dll_gate.exe "$@"
