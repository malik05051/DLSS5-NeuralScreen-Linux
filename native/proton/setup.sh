#!/usr/bin/env bash
# Set up the Proton side of the neural renderer, end to end:
#
#   1. build nvngx.dll_nr.exe (build.sh)
#   2. check that nvngx_dlssnr.dll is beside it
#   3. bootstrap the Wine prefix with umu-run, which is what copies the
#      driver's NGX core for Wine, vkd3d-proton, DXVK's dxgi and dxvk-nvapi
#      into it
#   4. run neuralscreen-host --probe, whose last line is the verdict
#
# Needs: a GE-Proton in ~/.local/share/Steam/compatibilitytools.d (or
# NS_PROTON_WINE pointing at any Proton's files/bin/wine), umu-launcher,
# mingw-w64-gcc, and the NVIDIA driver's Wine NGX (/usr/lib/nvidia/wine,
# in nvidia-utils on Arch).
#
# The prefix goes to ~/.local/share/neuralscreen/pfx unless NS_PROTON_PREFIX
# says otherwise - the same default the worker uses.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
prefix="${NS_PROTON_PREFIX:-$HOME/.local/share/neuralscreen/pfx}"

"$here/build.sh"

if [[ ! -f "$here/nvngx_dlssnr.dll" && -z "${NS_NR_DLL:-}" ]]; then
    cat >&2 <<MSG
error: $here/nvngx_dlssnr.dll is missing.

  It is NVIDIA's DLSS 5 neural-rendering runtime and is not distributed
  with this repository. The Windows project this port comes from ships it
  inside its release archive (native/nvngx_dlssnr.dll in
  https://github.com/perseval-BLR/DLSS5-NeuralScreen/releases). Copy that
  file here, or point NS_NR_DLL at it, and run this script again.
MSG
    exit 1
fi

if [[ ! -d "$prefix/drive_c" ]]; then
    command -v umu-run >/dev/null || { echo "error: umu-run not found - install umu-launcher" >&2; exit 1; }
    ls /usr/lib/nvidia/wine/_nvngx.dll >/dev/null 2>&1 || echo "warning: /usr/lib/nvidia/wine/_nvngx.dll not found - the driver's Wine NGX core is missing" >&2
    proton="${PROTONPATH:-}"
    if [[ -z "$proton" ]]; then
        # Newest GE-Proton by name; the worker picks the same one.
        proton="$(ls -d "$HOME"/.local/share/Steam/compatibilitytools.d/GE-Proton* 2>/dev/null | sort -V | tail -1 || true)"
        proton="${proton##*/}"
    fi
    [[ -n "$proton" ]] || { echo "error: no GE-Proton found under ~/.local/share/Steam/compatibilitytools.d" >&2; exit 1; }
    echo "bootstrapping the prefix at $prefix with $proton"
    mkdir -p "$(dirname "$prefix")"
    WINEPREFIX="$prefix" PROTONPATH="$proton" GAMEID=umu-neuralscreen STORE=none UMU_NO_PROTON_UPDATE=1 umu-run wineboot -u
    for dll in _nvngx.dll nvngx.dll nvapi64.dll d3d12.dll dxgi.dll; do
        [[ -f "$prefix/drive_c/windows/system32/$dll" ]] || echo "warning: $dll did not land in the prefix" >&2
    done
fi

host="$here/../linux/neuralscreen-host"
[[ -x "$host" ]] || { echo "error: $host is not built - run native/linux/build-host.sh" >&2; exit 1; }
echo
echo "probing:"
NS_PROTON_PREFIX="$prefix" "$host" --probe 2>/dev/null | grep -E '^(device|ngx|native verdict|proton)'
