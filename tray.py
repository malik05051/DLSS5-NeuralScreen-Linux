"""TrayController - a status icon, as StatusNotifierItem.

Right-click menu: NR ON/OFF (with a checkmark), Scale (state), Settings,
Scale +0.05 / -0.05, Exit. Left click on the icon is the default action =
open the menu. Commands go into a queue.Queue that the main loop drains.

There is no system tray on Wayland - no protocol for one, and no plan for
one. What every desktop implements instead is StatusNotifierItem: the
application registers an object on the session bus, the panel reads its
properties and draws the icon itself. KDE, every wlroots panel (waybar,
ironbar), Budgie and Cinnamon watch for it out of the box; GNOME needs the
AppIndicator extension, which is the single most-installed GNOME extension
and still an extension, so the program says so in the log rather than
leaving a user wondering where the icon went.

pystray is not used, although it has an AppIndicator backend: that backend
needs GTK3, PyGObject and the libappindicator typelib installed system-wide,
which is three dependencies to draw one icon. SNI is a D-Bus object with
seven properties and a menu, and dbusio already speaks D-Bus.

The icon is the channel avatar, native/neuralscreen.png. The Windows build
used a .ico because the tray asked for 16 or 24 px and the white rim that
separates the logo from a dark taskbar is a matter of one pixel at that
size. Here the panel scales the image itself and asks for whatever its own
scale factor wants, so the icon is sent as pixels at the largest size we
have and the panel does the rest.
"""

from __future__ import annotations

import os
import queue
import sys
import threading

from jeepney import DBusAddress, HeaderFields, MatchRule, MessageType, new_method_return, new_error

from dbusio import BUS, DBusError, variant
from paths import NATIVE_DIR

#: Fallback labels, used when the caller passes none.
DEFAULT_LABELS = {"settings": "Settings", "quit": "Exit"}

_ITEM_PATH = "/StatusNotifierItem"
_MENU_PATH = "/StatusNotifierItem/Menu"
_ITEM_IFACE = "org.kde.StatusNotifierItem"
_MENU_IFACE = "com.canonical.dbusmenu"
_WATCHER = "org.kde.StatusNotifierWatcher"


def _icon_pixmap(size: int = 64):
    """The icon as SNI wants it: (width, height, ARGB32 big-endian bytes).

    Big-endian is not a typo and not negotiable - the specification says so,
    and a panel fed little-endian draws the program's logo in the wrong
    colours, which looks like a broken icon rather than a byte-order bug.
    """
    try:
        from PIL import Image

        png = NATIVE_DIR / "neuralscreen.png"
        if png.is_file():
            img = Image.open(png).convert("RGBA")
        else:
            img = Image.new("RGBA", (size, size), (0x0D, 0x11, 0x17, 255))
            from PIL import ImageDraw
            d = size // 8
            ImageDraw.Draw(img).rectangle((d, d, size - d, size - d),
                                          fill=(0xFF, 0xBF, 0x00, 255))
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        rgba = img.tobytes()
        argb = bytearray(len(rgba))
        for i in range(0, len(rgba), 4):
            argb[i] = rgba[i + 3]          # A
            argb[i + 1] = rgba[i]          # R
            argb[i + 2] = rgba[i + 1]      # G
            argb[i + 3] = rgba[i + 2]      # B
        return (size, size, bytes(argb))
    except Exception as exc:
        print(f"[tray] could not build the icon: {exc}", file=sys.stderr)
        return (0, 0, b"")


class TrayController:
    """Status icon: commands into a queue, state for the menu to show.

    The same constructor and the same three methods the Windows version
    had, so main.py did not change: `start()`, `stop()`, and the state
    updates that keep the menu's checkmark and scale reading honest.
    """

    def __init__(self, commands: queue.Queue, labels: dict | None = None):
        self._commands = commands
        self._labels = dict(DEFAULT_LABELS, **(labels or {}))
        self._state = {"nr": True, "scale": 0.5}
        self._thread = None
        self._stop = threading.Event()
        self._registered = False
        self._revision = 1
        self._icon = _icon_pixmap()
        self._rules: list = []

    # -- state -------------------------------------------------------------

    def _set_state(self, **kw) -> None:
        self._state.update(kw)
        self._revision += 1
        self._emit_menu_changed()

    def _cmd(self, name: str) -> None:
        self._commands.put(name)

    # -- the menu, as com.canonical.dbusmenu -------------------------------

    def _layout(self):
        """The menu tree: (revision, root).

        A dbusmenu node is (id, properties, children). The panel asks for
        this whole thing every time the user opens the menu, which is why
        the state is read here rather than pushed - there is no such thing
        as a stale checkmark.
        """
        nr = self._state["nr"]
        scale = self._state["scale"]
        items = [
            self._node(1, {"label": "NR: ON", "toggle-type": "radio",
                           "toggle-state": 1 if nr else 0}),
            self._node(2, {"label": "NR: OFF", "toggle-type": "radio",
                           "toggle-state": 0 if nr else 1}),
            self._node(3, {"type": "separator"}),
            self._node(4, {"label": f"Scale: {scale:.2f}", "enabled": False}),
            self._node(5, {"label": "Scale +0.05"}),
            self._node(6, {"label": "Scale -0.05"}),
            self._node(7, {"type": "separator"}),
            self._node(8, {"label": self._labels["settings"]}),
            self._node(9, {"label": self._labels["quit"]}),
        ]
        root = (0, {"children-display": variant("s", "submenu")},
                [("(ia{sv}av)", item) for item in items])
        return (self._revision, root)

    @staticmethod
    def _node(ident: int, props: dict):
        typed = {}
        for key, value in props.items():
            if isinstance(value, bool):
                typed[key] = variant("b", value)
            elif isinstance(value, int):
                typed[key] = variant("i", value)
            else:
                typed[key] = variant("s", str(value))
        return (ident, typed, [])

    def _on_menu_event(self, ident: int) -> None:
        if ident in (1, 2):
            wanted = (ident == 1)
            if wanted != self._state["nr"]:
                self._set_state(nr=wanted)
                self._cmd("toggle")
        elif ident == 5:
            # Scale is NOT changed optimistically: only main knows the
            # bounds and the step (WORK_SCALE_MIN/MAX), and it may defer
            # applying because of the cooldown. The real value comes back
            # through set_scale().
            self._cmd("scale_up")
        elif ident == 6:
            self._cmd("scale_down")
        elif ident == 8:
            self._cmd("settings")
        elif ident == 9:
            self._cmd("quit")

    # -- the bus object ----------------------------------------------------

    def _properties(self) -> dict:
        nr = "ON" if self._state["nr"] else "OFF"
        return {
            "Category": variant("s", "ApplicationStatus"),
            "Id": variant("s", "neuralscreen"),
            "Title": variant("s", "NeuralScreen"),
            "Status": variant("s", "Active"),
            "IconName": variant("s", "neuralscreen"),
            "IconPixmap": variant("a(iiay)", [self._icon]),
            "ToolTip": variant("(sa(iiay)ss)",
                               ("neuralscreen", [], "NeuralScreen",
                                f"NR {nr} | scale {self._state['scale']:.2f}")),
            "ItemIsMenu": variant("b", False),
            "Menu": variant("o", _MENU_PATH),
        }

    def _reply(self, msg, signature: str, body: tuple) -> None:
        conn = BUS._conn
        if conn is None:
            return
        reply = new_method_return(msg, signature or None,
                                  body if signature else ())
        with BUS._lock:
            conn.send(reply, serial=next(conn.outgoing_serial))

    def _on_call(self, msg) -> None:
        """Serve the two interfaces the panel calls on us."""
        fields = msg.header.fields
        interface = fields.get(HeaderFields.interface, "")
        member = fields.get(HeaderFields.member, "")
        path = fields.get(HeaderFields.path, "")
        try:
            if interface == "org.freedesktop.DBus.Properties":
                self._on_properties(msg, member, path)
            elif interface == _ITEM_IFACE:
                self._on_item(msg, member)
            elif interface == _MENU_IFACE:
                self._on_menu(msg, member)
            elif interface == "org.freedesktop.DBus.Introspectable" \
                    and member == "Introspect":
                self._reply(msg, "s", (_INTROSPECT,))
        except Exception as exc:
            print(f"[tray] {interface}.{member} failed: {exc}", file=sys.stderr)

    def _on_properties(self, msg, member: str, path: str) -> None:
        if path == _MENU_PATH:
            props = {"Version": variant("u", 3),
                     "Status": variant("s", "normal"),
                     "TextDirection": variant("s", "ltr"),
                     "IconThemePath": variant("as", [])}
        else:
            props = self._properties()
        if member == "GetAll":
            self._reply(msg, "a{sv}", (props,))
        elif member == "Get":
            name = msg.body[1] if len(msg.body) > 1 else ""
            if name in props:
                self._reply(msg, "v", (props[name],))
            else:
                self._error(msg, "org.freedesktop.DBus.Error.UnknownProperty",
                            f"no property {name}")

    def _on_item(self, msg, member: str) -> None:
        if member in ("Activate", "SecondaryActivate"):
            # Activate is the left click. The Windows tray made the settings
            # item the default one for the same reason: left click for the
            # default action, right click for the menu.
            self._cmd("settings")
            self._reply(msg, "", ())
        elif member == "Scroll":
            delta = msg.body[0] if msg.body else 0
            self._cmd("scale_up" if delta > 0 else "scale_down")
            self._reply(msg, "", ())

    def _on_menu(self, msg, member: str) -> None:
        if member == "GetLayout":
            revision, root = self._layout()
            self._reply(msg, "u(ia{sv}av)", (revision, root))
        elif member == "AboutToShow":
            self._reply(msg, "b", (False,))
        elif member == "Event":
            ident = int(msg.body[0]) if msg.body else 0
            kind = msg.body[1] if len(msg.body) > 1 else ""
            if kind == "clicked":
                self._on_menu_event(ident)
            self._reply(msg, "", ())
        elif member == "GetGroupProperties":
            _revision, root = self._layout()
            entries = [(item[1][0], item[1][1]) for item in root[2]]
            self._reply(msg, "a(ia{sv})", (entries,))
        elif member == "EventGroup":
            self._reply(msg, "ai", ([],))

    def _error(self, msg, name: str, detail: str) -> None:
        conn = BUS._conn
        if conn is None:
            return
        with BUS._lock:
            conn.send(new_error(msg, name, "s", (detail,)),
                      serial=next(conn.outgoing_serial))

    def _emit_menu_changed(self) -> None:
        """Tell the panel the menu changed, so it re-reads the layout."""
        conn = BUS._conn
        if conn is None or not self._registered:
            return
        from jeepney import new_signal

        addr = DBusAddress(_MENU_PATH, interface=_MENU_IFACE)
        try:
            signal = new_signal(addr, "LayoutUpdated", "ui",
                                (self._revision, 0))
            with BUS._lock:
                conn.send(signal, serial=next(conn.outgoing_serial))
        except Exception:
            pass

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Register with the panel. Never raises - no tray is survivable."""
        if not BUS.connect():
            print("[tray] no session bus - no status icon", file=sys.stderr)
            return
        name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        if not BUS.request_name(name):
            print("[tray] could not take a bus name for the status icon",
                  file=sys.stderr)
            return
        for path, interfaces in ((_ITEM_PATH, (_ITEM_IFACE,
                                               "org.freedesktop.DBus.Properties",
                                               "org.freedesktop.DBus.Introspectable")),
                                 (_MENU_PATH, (_MENU_IFACE,
                                               "org.freedesktop.DBus.Properties"))):
            for interface in interfaces:
                rule = MatchRule(type="method_call", interface=interface,
                                 path=path)
                BUS.add_match(rule, self._on_call)
                self._rules.append(rule)
        watcher = DBusAddress("/StatusNotifierWatcher", bus_name=_WATCHER,
                              interface=_WATCHER)
        if not BUS.name_has_owner(_WATCHER):
            print("[tray] nothing on this desktop watches for status icons. "
                  "On GNOME that means the AppIndicator extension is not "
                  "installed; the menu is still on Num2 and the program runs "
                  "without an icon.", file=sys.stderr)
            return
        try:
            BUS.call(watcher, "RegisterStatusNotifierItem", "s", (name,),
                     timeout=10.0)
            self._registered = True
        except DBusError as exc:
            print(f"[tray] the panel refused the status icon: {exc}",
                  file=sys.stderr)

    def stop(self) -> None:
        self._stop.set()
        for rule in self._rules:
            BUS.remove_match(rule, self._on_call)
        self._rules.clear()
        self._registered = False


_INTROSPECT = (
    '<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN" '
    '"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">\n'
    '<node>'
    f'<interface name="{_ITEM_IFACE}">'
    '<method name="Activate"><arg type="i" direction="in"/>'
    '<arg type="i" direction="in"/></method>'
    '<method name="SecondaryActivate"><arg type="i" direction="in"/>'
    '<arg type="i" direction="in"/></method>'
    '<method name="Scroll"><arg type="i" direction="in"/>'
    '<arg type="s" direction="in"/></method>'
    '</interface>'
    f'<interface name="{_MENU_IFACE}">'
    '<method name="GetLayout">'
    '<arg type="i" direction="in"/><arg type="i" direction="in"/>'
    '<arg type="as" direction="in"/><arg type="u" direction="out"/>'
    '<arg type="(ia{sv}av)" direction="out"/></method>'
    '<method name="Event"><arg type="i" direction="in"/>'
    '<arg type="s" direction="in"/><arg type="v" direction="in"/>'
    '<arg type="u" direction="in"/></method>'
    '<method name="AboutToShow"><arg type="i" direction="in"/>'
    '<arg type="b" direction="out"/></method>'
    '</interface>'
    '</node>')
