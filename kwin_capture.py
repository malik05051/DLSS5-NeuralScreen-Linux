"""Keep our own overlay out of the screen capture, on KWin.

The program captures a monitor and draws the result back onto that same
monitor. Unless the compositor leaves our surface out of what it streams,
every frame the network runs on a picture that already contains its own
previous output: the loop compounds, and DLSS 5's temporal accumulation
turns it into smeared text and ghosted cursors within seconds (issue #9).

On Windows this was one flag, WDA_EXCLUDEFROMCAPTURE. Wayland has no
protocol for it - a client cannot ask to be left out of a screencast. But
KWin 6.6 grew the capability internally, as `excludeFromCapture` on its
Window class, for the "Hide from ScreenCast" menu item; layer surfaces
derive from that class and carry the property too. The only handles onto
it are a window rule, a context menu we do not have, and the scripting
API - so that is the one we use: load a two-line script that sets the
property on the window whose pid is ours, then unload it again. The
property lives on the window, not on the script, so unloading costs
nothing and leaves no scripts accumulating in the user's session.

Everything here degrades quietly. A compositor that is not KWin, a KWin
older than 6.6, a refused D-Bus call: the program keeps working exactly
as it did, and says once in the log that the overlay will be part of what
it captures.
"""
from __future__ import annotations

import os
import sys
import tempfile

import dbusio

# The script, with our pid baked in. windowList() includes layer-shell
# surfaces (ours reports layer 9, the overlay layer), so no window type or
# name matching is needed - and none would be reliable, because a layer
# surface has no app id and KWin falls back to the process name, which for
# every Python program on the machine is "python3.14".
_SCRIPT = """\
var pid = %d;
function mark(w) {
    if (w && w.pid === pid && !w.excludeFromCapture) {
        w.excludeFromCapture = true;
        print("neuralscreen: hid a surface from screencast");
    }
}
workspace.windowList().forEach(mark);
// And every surface this process maps from now on. The overlay is not on
// screen yet when the program asks - KWin lists a surface only once it is
// mapped - and it is destroyed and remade whenever the overlay moves to
// another monitor. A one-shot pass would lose both; this catches them.
workspace.windowAdded.connect(mark);
"""

_SCRIPTING = dbusio.DBusAddress("/Scripting", bus_name="org.kde.KWin",
                                interface="org.kde.kwin.Scripting")

#: Said once, however many times the overlay is rebuilt.
_warned = False


def available() -> bool:
    """Whether this looks like a session where the call can work."""
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP", "")
               + ":" + os.environ.get("XDG_SESSION_DESKTOP", "")).upper()
    return "KDE" in desktop or "PLASMA" in desktop


def exclude_self(pid: int | None = None) -> bool:
    """Ask KWin to leave this process's surfaces out of screencasts.

    True when KWin accepted the script. Call it again after the overlay
    surface is rebuilt (a monitor change destroys the layer surface, and
    the new one is a new Window with the property back at its default).
    """
    global _warned
    if pid is None:
        pid = os.getpid()
    path = ""
    try:
        if not available():
            raise RuntimeError("not a KWin session")
        # The bus is shared with the portal and the tray, and whoever gets
        # here first may be the one to open it - connect() returns True when
        # it is already open, so there is nothing to check first.
        if not dbusio.BUS.connect():
            raise RuntimeError("no session bus")
        fd, path = tempfile.mkstemp(prefix="ns-exclude-", suffix=".js")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_SCRIPT % int(pid))
        # loadScript returns the script's id; a name lets us unload it
        # again without guessing. Loading the same name twice is refused,
        # so the name carries the pid as well.
        name = f"neuralscreen-exclude-{pid}"
        try:
            dbusio.BUS.call(_SCRIPTING, "unloadScript", "s", (name,))
        except Exception:
            pass  # not loaded yet, which is the normal case
        dbusio.BUS.call(_SCRIPTING, "loadScript", "ss", (path, name))
        dbusio.BUS.call(_SCRIPTING, "start", "", ())
        return True
    except Exception as exc:
        if not _warned:
            _warned = True
            print(f"[main] the overlay cannot be hidden from the capture "
                  f"({exc}) - the picture it draws will be part of what it "
                  f"captures, which shows up as ghosting (issue #9). KWin "
                  f"6.6 or newer is needed.", file=sys.stderr)
        return False
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def release(pid: int | None = None) -> None:
    """Unload our script from KWin. Called when the program exits.

    A script left behind would sit in the session watching for windows of a
    pid that no longer exists - harmless, but litter. The next run unloads
    the same name before loading it again, so a crash costs nothing either.
    """
    if pid is None:
        pid = os.getpid()
    try:
        dbusio.BUS.call(_SCRIPTING, "unloadScript", "s",
                        (f"neuralscreen-exclude-{pid}",))
    except Exception:
        pass
