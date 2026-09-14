"""The window list, and where windows are - as far as Wayland will say.

This replaces winapi.py, and it is the one place where the port loses a
capability rather than trading it for an equivalent. On Windows,
EnumWindows handed any process the title, the class and the exact rectangle
of every window on the desktop, and `Num5` used WindowFromPoint to grab
whatever was under the cursor. Wayland does not have those calls, and the
omission is the entire point of the design: a client that could list and
locate other clients' windows could also watch them.

So what is here is three layers, tried in order, and the program says which
one it got rather than pretending they are the same:

  1. **ext-foreign-toplevel-list-v1** - a standard protocol, implemented by
     wlroots compositors, KDE and (since 48) GNOME. It gives a title, an
     app id and a stable handle for every toplevel. It gives no geometry,
     deliberately.

  2. **The compositor's own IPC** - sway's and Hyprland's socket. These do
     give geometry, and the window under the cursor, because a compositor is
     free to tell a privileged client anything. Used when it is there,
     never required.

  3. **Nothing** - and then the portal's own window picker does the choosing,
     which is the path GNOME takes. One extra click the first time; the
     restore token covers it afterwards.

Where the Windows build passed an `hwnd` integer around, this passes an
opaque handle - a string, unique per window, stable for that window's life.
Nothing outside this module looks inside it.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading

from wayland_shell import SHELL

try:
    from pywayland.protocol.ext_foreign_toplevel_list_v1 import (
        ExtForeignToplevelListV1)
except ImportError:      # pywayland older than the protocol
    ExtForeignToplevelListV1 = None


class Toplevel:
    """One window someone else owns."""

    def __init__(self, handle: str):
        self.handle = handle
        self.title = ""
        self.app_id = ""
        self.rect: tuple[int, int, int, int] | None = None
        self.focused = False
        self.minimised = False

    def __repr__(self) -> str:
        return f"<Toplevel {self.app_id or '?'}: {self.title!r}>"


# ---------------------------------------------------------------------------
# 1. ext-foreign-toplevel-list-v1
# ---------------------------------------------------------------------------


class _ForeignList:
    """The protocol's view: titles and app ids, no geometry, live.

    Bound lazily rather than in WaylandShell's registry handler, because
    most runs never open the window list and every toplevel on the desktop
    sends us two events per title change.
    """

    def __init__(self):
        self._manager = None
        self._windows: dict = {}       # proxy -> Toplevel
        self._lock = threading.Lock()
        self._tried = False
        self._counter = 0

    @property
    def available(self) -> bool:
        return self._manager is not None

    def start(self) -> bool:
        if self._manager is not None:
            return True
        if self._tried or ExtForeignToplevelListV1 is None:
            return False
        self._tried = True
        if SHELL.display is None and not SHELL.connect():
            return False
        # The registry has already been walked once; walk it again and bind
        # what we skipped the first time.
        registry = SHELL.display.get_registry()

        def _global(_registry, name, interface, version):
            if interface != "ext_foreign_toplevel_list_v1":
                return
            try:
                self._manager = _registry.bind(name, ExtForeignToplevelListV1,
                                               min(version, 1))
                self._manager.dispatcher["toplevel"] = self._on_toplevel
            except Exception as exc:
                print(f"[toplevels] could not bind the toplevel list: {exc}",
                      file=sys.stderr)

        registry.dispatcher["global"] = _global
        try:
            SHELL.display.roundtrip()
            if self._manager is not None:
                SHELL.display.roundtrip()   # the toplevels it just sent
        except Exception:
            return False
        return self._manager is not None

    def _on_toplevel(self, _manager, proxy) -> None:
        self._counter += 1
        entry = Toplevel(f"wl:{self._counter}")
        with self._lock:
            self._windows[proxy] = entry
        proxy.dispatcher["title"] = lambda _p, title: self._set(
            _p, "title", str(title))
        proxy.dispatcher["app_id"] = lambda _p, app_id: self._set(
            _p, "app_id", str(app_id))
        proxy.dispatcher["closed"] = self._on_closed

    def _set(self, proxy, field: str, value: str) -> None:
        with self._lock:
            entry = self._windows.get(proxy)
        if entry is not None:
            setattr(entry, field, value)

    def _on_closed(self, proxy) -> None:
        with self._lock:
            self._windows.pop(proxy, None)
        try:
            proxy.destroy()
        except Exception:
            pass

    def list(self) -> list[Toplevel]:
        if not self.start():
            return []
        try:
            SHELL.display.roundtrip()
        except Exception:
            pass
        with self._lock:
            return list(self._windows.values())


_FOREIGN = _ForeignList()


# ---------------------------------------------------------------------------
# 2. The compositor's own IPC
# ---------------------------------------------------------------------------


def _sway_ipc(kind: int, payload: bytes = b"") -> object | None:
    """One message on sway's (or i3's) socket. None when there is none.

    The protocol is a fixed header - magic, length, type - and a JSON body.
    Small enough to speak directly, and speaking it directly avoids
    depending on swaymsg being installed, which on a minimal install it is
    not.
    """
    path = os.environ.get("SWAYSOCK") or os.environ.get("I3SOCK")
    if not path or not os.path.exists(path):
        return None
    import struct
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(path)
            sock.sendall(b"i3-ipc" + struct.pack("=II", len(payload), kind)
                         + payload)
            header = _recv_exact(sock, 14)
            if header is None:
                return None
            length, _kind = struct.unpack("=II", header[6:14])
            body = _recv_exact(sock, length)
            return json.loads(body) if body else None
    except (OSError, ValueError):
        return None


def _recv_exact(sock, size: int) -> bytes | None:
    chunks = []
    left = size
    while left > 0:
        chunk = sock.recv(left)
        if not chunk:
            return None
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def _sway_windows() -> list[Toplevel]:
    tree = _sway_ipc(4)        # GET_TREE
    if not isinstance(tree, dict):
        return []
    out: list[Toplevel] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("type") in ("con", "floating_con") and node.get("name") \
                and not node.get("nodes") and not node.get("floating_nodes"):
            entry = Toplevel(f"sway:{node.get('id')}")
            entry.title = str(node.get("name") or "")
            entry.app_id = str(node.get("app_id")
                               or (node.get("window_properties") or {}).get("class")
                               or "")
            rect = node.get("rect") or {}
            if rect:
                entry.rect = (int(rect.get("x", 0)), int(rect.get("y", 0)),
                              int(rect.get("width", 0)), int(rect.get("height", 0)))
            entry.focused = bool(node.get("focused"))
            out.append(entry)
        for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
            walk(child)

    walk(tree)
    return out


def _hypr_ipc(command: str) -> object | None:
    """One `hyprctl -j` query, over the socket rather than the binary."""
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature:
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    for path in (f"{runtime}/hypr/{signature}/.socket.sock",
                 f"/tmp/hypr/{signature}/.socket.sock"):
        if not os.path.exists(path):
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(path)
                sock.sendall(f"j/{command}".encode())
                chunks = []
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            return json.loads(b"".join(chunks))
        except (OSError, ValueError):
            continue
    return None


def _hypr_windows() -> list[Toplevel]:
    clients = _hypr_ipc("clients")
    if not isinstance(clients, list):
        return []
    active = _hypr_ipc("activewindow")
    active_addr = active.get("address") if isinstance(active, dict) else None
    out: list[Toplevel] = []
    for client in clients:
        if not isinstance(client, dict) or not client.get("mapped", True):
            continue
        entry = Toplevel(f"hypr:{client.get('address')}")
        entry.title = str(client.get("title") or "")
        entry.app_id = str(client.get("class") or "")
        at, size = client.get("at") or [0, 0], client.get("size") or [0, 0]
        entry.rect = (int(at[0]), int(at[1]), int(size[0]), int(size[1]))
        entry.focused = client.get("address") == active_addr
        entry.minimised = bool(client.get("hidden"))
        out.append(entry)
    return out


def _ipc_windows() -> list[Toplevel]:
    return _hypr_windows() or _sway_windows()


def geometry_source() -> str:
    """Which layer can answer "where is that window": 'hyprland', 'sway', ''.

    The menu uses this to decide whether one-window mode can follow a
    window's position or has to leave the overlay where it is. Saying so is
    better than a window-follow that silently does nothing.
    """
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and _hypr_ipc("version"):
        return "hyprland"
    if (os.environ.get("SWAYSOCK") or os.environ.get("I3SOCK")) \
            and _sway_ipc(4) is not None:
        return "sway"
    return ""


# ---------------------------------------------------------------------------
# What the rest of the program calls
# ---------------------------------------------------------------------------


def list_capturable_windows() -> list[tuple[str, str]]:
    """Windows that can be captured: (handle, title).

    The window list for the menu. Windows with no title are skipped - they
    are the helper surfaces that the Windows build filtered out by
    WS_EX_TOOLWINDOW and the DWM cloak, and having no title is the Wayland
    equivalent signal.

    Our own overlay is not in the list: it is a layer surface, and layer
    surfaces are not toplevels. That is one whole category of bug the
    Windows version had to write code against - capturing your own overlay
    and getting a hall of mirrors - which cannot happen here.

    An empty list means neither the protocol nor the compositor's IPC is
    available, and the caller should fall through to the portal's own
    picker. It does not mean there are no windows.
    """
    entries = _ipc_windows() or _FOREIGN.list()
    out = []
    for entry in entries:
        title = entry.title.strip()
        if not title or entry.app_id == "neuralscreen":
            continue
        out.append((entry.handle, title))
    return out


def window_frame_rect(handle: str) -> tuple[int, int, int, int] | None:
    """The window's bounds as the compositor sees them: (x, y, w, h).

    None when the window is gone - and also None when this compositor does
    not tell clients where windows are, which is the standard case. Callers
    treat the two the same: leave the overlay where it is.
    """
    for entry in _ipc_windows():
        if entry.handle == handle:
            return entry.rect
    return None


def foreign_foreground() -> str:
    """The focused window, unless it is ours. "" when there is none.

    "Ours" cannot be the focused window here - the overlay is a layer
    surface and takes the keyboard without ever becoming a toplevel - so
    unlike the Windows version this needs no self-check beyond the app id.
    """
    for entry in _ipc_windows():
        if entry.focused and entry.app_id != "neuralscreen":
            return entry.handle
    return ""


def window_under_cursor() -> str:
    """The window under the pointer, "" when none or the compositor is quiet.

    Hyprland answers this directly. sway does not have a "what is at this
    point" query, so the focused window is used instead - which is what the
    user means nine times in ten, since pointing at a window in a tiling
    compositor usually focuses it.
    """
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        cursor = _hypr_ipc("cursorpos")
        clients = _hypr_windows()
        if isinstance(cursor, dict) and clients:
            x, y = int(cursor.get("x", 0)), int(cursor.get("y", 0))
            for entry in clients:
                if entry.rect is None:
                    continue
                wx, wy, ww, wh = entry.rect
                if wx <= x < wx + ww and wy <= y < wy + wh:
                    return entry.handle
    return foreign_foreground()


def window_title(handle: str) -> str:
    for entry in _ipc_windows() or _FOREIGN.list():
        if entry.handle == handle:
            return entry.title
    return ""
