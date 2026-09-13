#!/usr/bin/env bash
# Build neuralscreen-host, the GPU half of the program.
#
# The Windows build needed the Visual C++ toolchain, the Windows SDK and
# C++/WinRT; this needs g++ or clang++, the Vulkan headers and PipeWire's.
# On a distribution that is:
#
#   Debian/Ubuntu   apt install build-essential libvulkan-dev libpipewire-0.3-dev
#   Fedora          dnf install gcc-c++ vulkan-loader-devel pipewire-devel
#   Arch            pacman -S base-devel vulkan-headers libpipewire
#
# NGX itself is not a build dependency: the headers travel with the source
# (native/include), and libnvidia-ngx comes with the NVIDIA driver, so the
# host loads it at run time. What does have to be there at run time is
# nvngx_dlssnr.so next to the binary - the neural renderer's own snippet,
# which is the one file this project cannot ship for you.
#
#   ./build-host.sh            release
#   ./build-host.sh --debug    with symbols and no optimisation
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

CXX="${CXX:-g++}"
OUT="neuralscreen-host"

flags=(-std=c++17 -Wall -Wextra -Wno-unused-parameter -I../include)
if [[ "${1:-}" == "--debug" ]]; then
    flags+=(-O0 -g)
else
    # No -march=native: the release archive is built on one machine and run
    # on somebody else's, and an illegal instruction at startup is a very
    # confusing way to learn that.
    flags+=(-O2 -DNDEBUG)
fi

if ! pkg-config --exists libpipewire-0.3; then
    echo "error: libpipewire-0.3 development files are missing" >&2
    echo "       (see the distribution list at the top of this script)" >&2
    exit 1
fi
flags+=($(pkg-config --cflags libpipewire-0.3))

libs=($(pkg-config --libs libpipewire-0.3) -lvulkan -lpthread -ldl -lrt)

# rpath $ORIGIN: the host loads nvngx_dlssnr.so from beside itself, exactly
# as the Windows build loaded the DLL from its own directory, and without
# this it would only be found if the user happened to set LD_LIBRARY_PATH.
libs+=(-Wl,-rpath,'$ORIGIN')

echo "building $OUT with $CXX"
"$CXX" "${flags[@]}" host.cpp ns_vk.cpp ns_pw.cpp -o "$OUT" "${libs[@]}"
echo "built $here/$OUT"
echo
echo "check it against your card with:  ./$OUT --probe"
