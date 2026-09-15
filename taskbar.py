"""TaskbarWindow - the program's entry in the desktop's application list.

The Windows version of this file opened a real 1x1 top-level window with
WS_EX_APPWINDOW, parked at a corner of the screen, purely so the program
would have a taskbar button: the overlay was click-through and the worker's
window was a tool window, so neither showed up, and the program lived only
in the tray.

None of that is possible here and none of it is necessary. A Wayland
compositor builds its window list from toplevel surfaces, and our overlay is
a layer surface on purpose - a layer surface is not a window, has no title
bar, and cannot be in a taskbar. Opening a decoy toplevel to get an entry
would give the user a blank window they can focus, move and close, which is
worse than no entry at all.

What a Linux desktop actually reads for "this program exists" is the desktop
entry: a .desktop file in the applications directory. That gives the
launcher an icon, a name and a way to start the program, which is what the
taskbar button was standing in for. So this class installs one and keeps the
constructor, `start()`, `stop()` and `hwnd()` that main.py calls, doing
nothing where nothing is the right thing to do.

The status icon (tray.py) remains the way to reach a running instance.
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
from pathlib import Path

from paths import BASE_DIR, NATIVE_DIR, _xdg


APP_ID = "neuralscreen"

#: ~/.local/share/applications - where a desktop entry goes for one user.
APPLICATIONS_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "applications"
ICONS_DIR = (_xdg("XDG_DATA_HOME", ".local/share") / "icons" / "hicolor"
             / "256x256" / "apps")

DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Name=NeuralScreen
GenericName=Neural desktop renderer
Comment=Run the whole desktop through NVIDIA's DLSS 5 neural renderer
Exec={exec}
Icon={icon}
Terminal=false
Categories=Graphics;Utility;
StartupNotify=false
StartupWMClass=neuralscreen
X-GNOME-UsesNotifications=false
"""


def launcher_path() -> Path:
    """The script the desktop entry runs.

    NEURALSCREEN_LAUNCHER overrides it, which is what a distribution package
    sets. There the program lives under a read-only /usr/share and the thing
    a user runs is /usr/bin/neuralscreen; without the override an autostart
    entry written from the menu would point into the package's own directory
    and miss the wrapper that locates NVIDIA's runtime.
    """
    override = os.environ.get("NEURALSCREEN_LAUNCHER", "")
    return Path(override) if override else BASE_DIR / "neuralscreen.sh"


def install_desktop_entry(autostart: bool = False) -> Path | None:
    """Write the .desktop file, and the icon it points at. None on failure.

    Writing it every launch rather than at install time is deliberate: the
    archive is unpacked anywhere and has no installer, exactly as the
    Windows build had none, so the Exec line has to be built from where the
    program actually is right now. A user who moves the folder gets a
    corrected entry on the next launch instead of a launcher that silently
    stops working.
    """
    try:
        APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
        icon = NATIVE_DIR / "neuralscreen.png"
        icon_value = APP_ID
        if icon.is_file():
            try:
                ICONS_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(icon, ICONS_DIR / f"{APP_ID}.png")
            except OSError:
                # An icon theme directory we cannot write to is not a reason
                # to have no launcher: point the entry straight at the file.
                icon_value = str(icon)
        else:
            icon_value = str(icon)
        target = APPLICATIONS_DIR / f"{APP_ID}.desktop"
        text = DESKTOP_ENTRY.format(exec=launcher_path(), icon=icon_value)
        if autostart:
            text += "X-GNOME-Autostart-enabled=true\n"
        target.write_text(text, encoding="utf-8")
        target.chmod(0o755)
        return target
    except OSError as exc:
        print(f"[taskbar] could not write the desktop entry: {exc}",
              file=sys.stderr)
        return None


class TaskbarWindow:
    """The desktop entry, behind the interface the Windows taskbar had.

    `commands` is accepted and unused: on Windows the taskbar button's
    activation was turned into the "settings" command, and here there is no
    button to activate. The status icon raises the menu instead.
    """

    def __init__(self, commands: queue.Queue, title: str = "NeuralScreen"):
        self._commands = commands
        self._title = title
        self.entry: Path | None = None

    def start(self) -> None:
        self.entry = install_desktop_entry()
        if self.entry is not None:
            print(f"[taskbar] desktop entry at {self.entry}")

    def stop(self) -> None:
        """Deliberately leaves the entry in place.

        Removing it on exit would mean the program only appears in the
        launcher while it is already running, which is the opposite of what
        a launcher is for. `neuralscreen.sh --uninstall` removes it.
        """

    def hwnd(self):
        """There is no window. None, and every caller checks."""
        return None


def remove_desktop_entry() -> None:
    """Take the launcher and the icon back out - for --uninstall."""
    for path in (APPLICATIONS_DIR / f"{APP_ID}.desktop",
                 ICONS_DIR / f"{APP_ID}.png"):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[taskbar] could not remove {path}: {exc}", file=sys.stderr)
