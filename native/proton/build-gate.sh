#!/usr/bin/env bash
# Build the DLSSNR feasibility gate: a Windows exe that runs nvngx_dlssnr.dll
# through Proton on this machine. Needs mingw-w64-gcc.
#
#   ./build-gate.sh            -> nvngx.dll_gate.exe (the name must carry
#                                 "nvngx.dll": the runtime checks its caller)
# Run it from a directory holding nvngx_dlssnr.dll, with GE-Proton's wine and
# DXVK_NVAPI_GPU_ARCH=GB200 on a pre-Blackwell card - see run-gate.sh.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
x86_64-w64-mingw32-g++ -std=c++17 -O2 -w -I../include nvngx.dll_gate.cpp \
    -o nvngx.dll_gate.exe -static -lole32
echo "built $PWD/nvngx.dll_gate.exe"
