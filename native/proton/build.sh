#!/usr/bin/env bash
# Build the Windows side of the neural renderer with MinGW:
#
#   nvngx.dll_nr.exe    the server the Linux worker talks to (ns_proton.cpp)
#   nvngx.dll_gate.exe  the standalone feasibility test (--probe does the
#                       same through the worker; this one needs nothing else)
#
# Both names carry "nvngx.dll" because the runtime refuses calls from a
# module whose path lacks that substring. Static CRT, so the prefix needs
# no MinGW DLLs.
#
#   Arch     pacman -S mingw-w64-gcc
#   Debian   apt install g++-mingw-w64-x86-64
#   Fedora   dnf install mingw64-gcc-c++
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
CXX="${MINGW_CXX:-x86_64-w64-mingw32-g++}"
command -v "$CXX" >/dev/null || { echo "error: $CXX not found (see the distribution list at the top of this script)" >&2; exit 1; }
flags=(-std=c++17 -O2 -Wall -Wno-cast-function-type -I../include -static -lole32)
echo "building nvngx.dll_nr.exe with $CXX"
"$CXX" nvngx.dll_nr.cpp -o nvngx.dll_nr.exe "${flags[@]}"
echo "building nvngx.dll_gate.exe with $CXX"
"$CXX" -w nvngx.dll_gate.cpp -o nvngx.dll_gate.exe "${flags[@]}"
echo "built $PWD/nvngx.dll_nr.exe and nvngx.dll_gate.exe"
