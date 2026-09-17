"""Where the program's own files live.

One place, imported by everything that needs a path and importing nothing
itself - which is what keeps it out of the import cycles the rest of the
split had to avoid.
"""
from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


NATIVE_DIR = BASE_DIR / "native"


# The neural renderer. NVIDIA ships DLSSNR only as a D3D12 DLL and there is
# no Linux snippet for NGX to load, so the network runs in a small Windows
# program under Proton: native/proton/nvngx.dll_nr.exe, with NVIDIA's
# nvngx_dlssnr.dll beside it. The worker starts that process itself
# (native/linux/ns_proton.cpp); these paths are what the launcher and the
# packager check for. The DLL's name is part of the contract - NGX derives
# it from the feature id - and the exe's name must contain "nvngx.dll",
# because the runtime refuses calls from any other module.
PROTON_DIR = NATIVE_DIR / "proton"
NR_EXE = PROTON_DIR / "nvngx.dll_nr.exe"
NR_SNIPPET = PROTON_DIR / "nvngx_dlssnr.dll"


# The worker is an ordinary ELF executable here; the Windows build had to
# disguise it as nvngx.dll to get past NGX Core's process-name check.
WORKER_EXE = NATIVE_DIR / "linux" / "neuralscreen-host"


def _xdg(var: str, default: str) -> Path:
    """An XDG base directory, honouring the environment, never relative.

    The spec says a relative value is to be ignored as if it were unset -
    a program that resolves it against its own cwd writes the user's
    config into whatever directory it happened to be launched from.
    """
    raw = os.environ.get(var, "")
    path = Path(raw) if raw else Path()
    if not path.is_absolute():
        path = Path.home() / default
    return path


#: ~/.config/neuralscreen - config.json, presets, the saved menu layout.
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "neuralscreen"

#: ~/.local/state/neuralscreen - the log. State, not config: it is written
#: by the program and never edited by hand.
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state") / "neuralscreen"

#: ~/.config/autostart - where a .desktop file makes the program start with
#: the session. The one thing written outside the program's own directories.
AUTOSTART_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "autostart"

AUTOSTART_FILE = AUTOSTART_DIR / "neuralscreen.desktop"


def user_dir(kind: str, fallback: str) -> Path:
    """A user directory from user-dirs.dirs (XDG_VIDEOS_DIR, ...).

    Recordings belong in the user's Videos folder and screenshots in
    Pictures - a program that writes into its own installation directory is
    a Windows habit that does not survive a read-only /opt or a Flatpak.
    The file is a tiny shell fragment; only the assignments are read, and
    anything unparseable falls back to the English name under $HOME.
    """
    conf = _xdg("XDG_CONFIG_HOME", ".config") / "user-dirs.dirs"
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith(f"{kind}="):
                continue
            value = line.split("=", 1)[1].strip().strip('"')
            value = value.replace("$HOME", str(Path.home()))
            path = Path(value)
            if path.is_absolute():
                return path
    except Exception:
        pass
    return Path.home() / fallback


def config_path() -> Path:
    """Where config.json is read from and written to.

    The archive is unpacked anywhere and stays self-contained, so a
    config.json sitting next to main.py wins - that is the portable install
    and the one the Windows build had. When there is none, the settings go
    to ~/.config/neuralscreen, which is the only writable place left once
    the program lives in /opt or /usr/share.
    """
    beside = BASE_DIR / "config.json"
    if beside.is_file():
        return beside
    return CONFIG_DIR / "config.json"


def log_path() -> Path:
    """Where NeuralScreen.log goes: beside the program, else ~/.local/state."""
    if os.access(BASE_DIR, os.W_OK):
        return BASE_DIR / "NeuralScreen.log"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / "NeuralScreen.log"
