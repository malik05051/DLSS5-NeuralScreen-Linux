#!/usr/bin/env bash
# NeuralScreen - the DLSS 5 neural renderer over the whole Wayland desktop.
#
# This is the launcher: it checks what has to be there, builds the worker on
# first run, and starts the program. Run it from a terminal and the log goes
# there as well as into NeuralScreen.log; run it from the desktop entry and
# the log is the only copy.
#
#   ./neuralscreen.sh              run
#   ./neuralscreen.sh --diag       run with the worker's phase profiler on
#   ./neuralscreen.sh --install    write the desktop entry and exit
#   ./neuralscreen.sh --uninstall  remove the desktop entry and autostart
#
# The Windows build had two launchers - a .bat that kept a console and a
# .vbs that did not - because Windows makes that a choice you have to make
# up front. Here it is one script: a terminal gets the output, a desktop
# entry does not, and neither needs a separate file.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

say() { printf '[NeuralScreen] %s\n' "$*" >&2; }

die() {
    say "$*"
    # notify-send when there is no terminal: launched from the desktop this
    # is the only way the user ever learns why nothing happened.
    if [[ ! -t 2 ]] && command -v notify-send >/dev/null 2>&1; then
        notify-send --icon=neuralscreen --urgency=critical \
            "NeuralScreen did not start" "$*"
    fi
    exit 1
}

# --- what the program needs ------------------------------------------------

case "${1:-}" in
--install)
    exec "${NEURALSCREEN_PYTHON:-python3}" -c \
        "import sys; sys.path.insert(0, '$here'); import taskbar; \
         print(taskbar.install_desktop_entry() or 'failed')"
    ;;
--uninstall)
    "${NEURALSCREEN_PYTHON:-python3}" -c \
        "import sys; sys.path.insert(0, '$here'); import taskbar, paths; \
         taskbar.remove_desktop_entry(); \
         paths.AUTOSTART_FILE.unlink(missing_ok=True)"
    say "desktop entry and autostart removed"
    exit 0
    ;;
esac

if [[ "${XDG_SESSION_TYPE:-}" != "wayland" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    die "this is a Wayland program and there is no Wayland session here
(WAYLAND_DISPLAY is not set). Log in to a Wayland session and try again."
fi

# Python: the environment variable wins, then a bundled runtime next to the
# program, then whatever is on PATH.
PY="${NEURALSCREEN_PYTHON:-}"
if [[ -z "$PY" && -x "$here/runtime/bin/python3" ]]; then
    PY="$here/runtime/bin/python3"
fi
[[ -z "$PY" ]] && PY="python3"
command -v "$PY" >/dev/null 2>&1 || die "python3 not found (looked for '$PY')"

# The neural renderer runs through Proton (NVIDIA ships it only as a D3D12
# DLL): our nvngx.dll_nr.exe plus NVIDIA's own nvngx_dlssnr.dll, 165 MB and
# not redistributable through this repository. Without them the program
# starts and the picture is never processed, which is a worse failure than
# not starting - so they are checked. NS_NR_DLL points at a runtime kept
# elsewhere; native/proton/setup.sh builds the rest and prepares the prefix.
if [[ ! -f "$here/native/proton/nvngx.dll_nr.exe" ]]; then
    die "native/proton/nvngx.dll_nr.exe is not built.
Run native/proton/setup.sh - see README.md, the section 'What you need'."
fi
if [[ ! -f "${NS_NR_DLL:-$here/native/proton/nvngx_dlssnr.dll}" ]]; then
    die "native/proton/nvngx_dlssnr.dll is missing.
See README.md, the section 'What you need' - it is NVIDIA's neural
renderer runtime and has to be put there by hand (or NS_NR_DLL set)."
fi

# The worker: a build artefact, not stored in git. Built once, here.
if [[ ! -x "$here/native/linux/neuralscreen-host" ]]; then
    say "the worker is not built yet - building it"
    if ! "$here/native/linux/build-host.sh"; then
        die "the worker did not build. See native/linux/build-host.sh for the
packages your distribution needs."
    fi
fi

# --- the Python side's own dependencies ------------------------------------

missing=$("$PY" - <<'EOF'
import importlib
need = {"numpy": "numpy", "cv2": "opencv-python-headless", "pygame": "pygame",
        "av": "av", "PIL": "pillow", "jeepney": "jeepney",
        "pywayland": "pywayland"}
print(" ".join(pkg for mod, pkg in need.items()
               if importlib.util.find_spec(mod) is None))
EOF
)
if [[ -n "${missing// /}" ]]; then
    die "these Python packages are missing: $missing
Install them with:  $PY -m pip install --user $missing"
fi

# --- run -------------------------------------------------------------------

if [[ "${1:-}" == "--diag" ]]; then
    # The worker's phase profiler: the log then carries [phase]/[host] lines
    # that say exactly where a frame went. This is what to run when a bug
    # report asks for a diagnostic log.
    export NS_PHASE=1
    shift
    say "diagnostic mode: NS_PHASE=1"
fi

exec "$PY" "$here/main.py" "$@"
