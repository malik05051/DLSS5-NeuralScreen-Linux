"""xdg-desktop-portal: the desktop's answer to everything Win32 used to do
directly.

On Windows a program that wanted the screen called Desktop Duplication, a
program that wanted a hotkey called RegisterHotKey, and a program that
wanted a file dialog called GetSaveFileName. None of that exists on Wayland
by design: a client cannot see other clients' pixels, cannot take a key away
from the compositor, and does not draw system dialogs. What replaces all
three is one D-Bus service that asks the user once and then hands over a
capability.

That changes one thing the rest of the program has to live with, and it is
worth stating plainly rather than hiding behind a wrapper: **the user is in
the loop.** The first capture shows a picker. The first hotkey binding shows
a shortcuts dialog. Neither can be done silently, and no amount of API
design makes it otherwise - that is the security model, not a missing
feature.

What we can do is ask exactly once. Every session here is created with
`persist_mode=2` and remembers its `restore_token` in the config, so the
second launch restores the same screen with no dialog at all. A token that
the portal rejects (the monitor is gone, the user revoked it) is dropped and
the picker comes back - never an error to the user.

Interfaces used:
    ScreenCast       - the capture, as a PipeWire node   (replaces DDA/WGC)
    GlobalShortcuts  - the hotkeys                       (replaces RegisterHotKey)
    FileChooser      - Save As and the folder picker     (replaces GetSaveFileName)
    Settings         - the desktop's light/dark preference
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Callable
from urllib.parse import unquote, urlparse

from jeepney import MatchRule

from dbusio import BUS, DBusError, PortalCall, fd_arg, portal, unwrap, variant


# ScreenCast source types (a bitmask).
SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
SOURCE_VIRTUAL = 4

# Cursor modes. EMBEDDED draws the pointer into the frame, which is what the
# Windows build did by compositing the cursor itself - and what the neural
# renderer should see, since the user sees it too.
CURSOR_HIDDEN = 1
CURSOR_EMBEDDED = 2
CURSOR_METADATA = 4

# persist_mode: 0 none, 1 until the app stops, 2 until the user revokes it.
PERSIST_PERMANENT = 2


def _version(interface: str) -> int:
    """The portal's version of one interface, 0 when it is not there.

    Worth asking rather than assuming: GlobalShortcuts landed in portal
    1.16 and several backends still ship without it, and the difference
    between "your compositor has no global shortcuts" and a call that hangs
    for 25 seconds is this property read.
    """
    from jeepney import DBusAddress
    props = DBusAddress("/org/freedesktop/portal/desktop",
                        bus_name="org.freedesktop.portal.Desktop",
                        interface="org.freedesktop.DBus.Properties")
    try:
        reply = BUS.call(props, "Get", "ss",
                         (f"org.freedesktop.portal.{interface}", "version"),
                         timeout=5.0)
    except DBusError:
        return 0
    return int(unwrap(reply)[0] or 0)


def available(interface: str) -> bool:
    return _version(interface) > 0


# ---------------------------------------------------------------------------
# ScreenCast
# ---------------------------------------------------------------------------


class ScreenCastSession:
    """A negotiated capture: one PipeWire node and the fd to read it on.

    The session stays open for as long as the capture does - closing it is
    what tells the compositor to stop producing frames, and a session leaked
    across a pipeline rebuild is a second copy of the screen being composited
    for nobody.
    """

    def __init__(self):
        self.session_handle = ""
        self.node_id = -1
        self.fd = -1
        #: Give this back to `open()` next time and the user is not asked.
        self.restore_token = ""
        #: The captured area in compositor coordinates, when the portal says.
        self.position = (0, 0)
        self.size = (0, 0)
        self.source_type = 0
        #: A stable id for the source, on portals new enough to give one.
        self.mapping_id = ""

    @property
    def ok(self) -> bool:
        return self.node_id >= 0 and self.fd >= 0

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        if self.session_handle:
            from jeepney import DBusAddress
            addr = DBusAddress(self.session_handle,
                               bus_name="org.freedesktop.portal.Desktop",
                               interface="org.freedesktop.portal.Session")
            try:
                BUS.call(addr, "Close", timeout=5.0)
            except DBusError:
                pass
            self.session_handle = ""
        self.node_id = -1


def open_screencast(source_types: int = SOURCE_MONITOR,
                    restore_token: str = "",
                    cursor: int = CURSOR_EMBEDDED,
                    timeout: float = 120.0) -> ScreenCastSession:
    """Negotiate a capture. Returns a session; check `.ok`.

    `timeout` is two minutes because Start() puts a picker in front of the
    user and a person choosing a window is not on a computer's schedule.
    With a valid `restore_token` nothing is shown and it returns in
    milliseconds.
    """
    out = ScreenCastSession()
    if not BUS.connect():
        print("[portal] no session bus - no screen capture", file=sys.stderr)
        return out
    addr = portal("ScreenCast")

    # 1. CreateSession
    with PortalCall(BUS, "ns_cast") as req:
        token = f"nssession_{os.getpid()}"
        try:
            BUS.call(addr, "CreateSession", "a{sv}",
                     ({"handle_token": variant("s", req.token),
                       "session_handle_token": variant("s", token)},))
        except DBusError as exc:
            print(f"[portal] ScreenCast unavailable: {exc}", file=sys.stderr)
            return out
        code, results = req.wait(30.0)
    if code != 0:
        print(f"[portal] CreateSession refused (code {code})", file=sys.stderr)
        return out
    out.session_handle = results.get("session_handle", "")
    if not out.session_handle:
        return out

    # 2. SelectSources. A restore_token the portal does not recognise is not
    #    an error: it falls back to showing the picker, which is exactly what
    #    we want when the remembered monitor has been unplugged.
    options = {
        "handle_token": None,  # filled in below
        "types": variant("u", source_types),
        "multiple": variant("b", False),
        "cursor_mode": variant("u", cursor),
        "persist_mode": variant("u", PERSIST_PERMANENT),
    }
    if restore_token:
        options["restore_token"] = variant("s", restore_token)
    with PortalCall(BUS, "ns_sel") as req:
        options["handle_token"] = variant("s", req.token)
        try:
            BUS.call(addr, "SelectSources", "oa{sv}",
                     (out.session_handle, options))
        except DBusError as exc:
            print(f"[portal] SelectSources failed: {exc}", file=sys.stderr)
            out.close()
            return out
        code, _ = req.wait(timeout)
    if code != 0:
        print(f"[portal] the capture was not granted (code {code})",
              file=sys.stderr)
        out.close()
        return out

    # 3. Start - this is the one that may show the picker.
    with PortalCall(BUS, "ns_start") as req:
        try:
            BUS.call(addr, "Start", "osa{sv}",
                     (out.session_handle, "",
                      {"handle_token": variant("s", req.token)}),
                     timeout=timeout)
        except DBusError as exc:
            print(f"[portal] Start failed: {exc}", file=sys.stderr)
            out.close()
            return out
        code, results = req.wait(timeout)
    if code != 0:
        print(f"[portal] the user did not grant the capture (code {code})",
              file=sys.stderr)
        out.close()
        return out

    out.restore_token = results.get("restore_token", "") or ""
    streams = results.get("streams") or []
    if not streams:
        print("[portal] the portal granted no stream", file=sys.stderr)
        out.close()
        return out
    node_id, props = streams[0][0], streams[0][1]
    out.node_id = int(node_id)
    props = unwrap(props)
    if isinstance(props.get("position"), (list, tuple)):
        out.position = (int(props["position"][0]), int(props["position"][1]))
    if isinstance(props.get("size"), (list, tuple)):
        out.size = (int(props["size"][0]), int(props["size"][1]))
    out.source_type = int(props.get("source_type") or 0)
    out.mapping_id = str(props.get("mapping_id") or "")

    # 4. The PipeWire remote itself, as a file descriptor. This is the one
    #    reply whose payload is not in the body.
    try:
        reply = BUS.call(addr, "OpenPipeWireRemote", "oa{sv}",
                         (out.session_handle, {}), timeout=15.0)
        out.fd = fd_arg(reply, 0)
    except Exception as exc:
        print(f"[portal] OpenPipeWireRemote failed: {exc}", file=sys.stderr)
        out.close()
        return out
    return out


# ---------------------------------------------------------------------------
# GlobalShortcuts
# ---------------------------------------------------------------------------


class ShortcutsSession:
    """Global shortcuts, delivered as Activated/Deactivated signals.

    The portal's model is deliberately weaker than RegisterHotKey's: we say
    which actions exist and what we would *prefer* they be bound to, and the
    compositor - or the user, in its settings - decides what they actually
    are. So `Num1` in the config is a preferred trigger, not a promise, and
    `current()` reports back what the desktop really assigned so the menu can
    show the truth instead of our wish.
    """

    def __init__(self, on_activated: Callable[[str], None]):
        self._on_activated = on_activated
        self.session_handle = ""
        self.bound: dict[str, str] = {}   # id -> the trigger the desktop chose
        self._rules: list[tuple[MatchRule, Callable]] = []
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> bool:
        if not BUS.connect():
            return False
        if not available("GlobalShortcuts"):
            print("[portal] this desktop has no GlobalShortcuts portal",
                  file=sys.stderr)
            return False
        addr = portal("GlobalShortcuts")
        with PortalCall(BUS, "ns_sc") as req:
            try:
                BUS.call(addr, "CreateSession", "a{sv}",
                         ({"handle_token": variant("s", req.token),
                           "session_handle_token":
                               variant("s", f"nsshortcuts_{os.getpid()}")},))
            except DBusError as exc:
                print(f"[portal] no shortcuts session: {exc}", file=sys.stderr)
                return False
            code, results = req.wait(30.0)
        if code != 0:
            return False
        self.session_handle = results.get("session_handle", "")
        if not self.session_handle:
            return False
        self._subscribe()
        return True

    def _subscribe(self) -> None:
        for member, handler in (("Activated", self._activated),
                                ("Deactivated", None),
                                ("ShortcutsChanged", self._changed)):
            if handler is None:
                continue
            rule = MatchRule(type="signal",
                             interface="org.freedesktop.portal.GlobalShortcuts",
                             member=member, path="/org/freedesktop/portal/desktop")
            BUS.add_match(rule, handler)
            self._rules.append((rule, handler))

    def _activated(self, msg) -> None:
        try:
            session, shortcut_id = msg.body[0], msg.body[1]
        except Exception:
            return
        if session != self.session_handle:
            return
        self._on_activated(str(shortcut_id))

    def _changed(self, msg) -> None:
        try:
            session, shortcuts = msg.body[0], msg.body[1]
        except Exception:
            return
        if session != self.session_handle:
            return
        with self._lock:
            self.bound = _trigger_map(shortcuts)

    def close(self) -> None:
        for rule, handler in self._rules:
            BUS.remove_match(rule, handler)
        self._rules.clear()
        if self.session_handle:
            from jeepney import DBusAddress
            addr = DBusAddress(self.session_handle,
                               bus_name="org.freedesktop.portal.Desktop",
                               interface="org.freedesktop.portal.Session")
            try:
                BUS.call(addr, "Close", timeout=5.0)
            except DBusError:
                pass
            self.session_handle = ""

    # -- binding -----------------------------------------------------------

    def bind(self, shortcuts: list[tuple[str, str, str]],
             timeout: float = 120.0) -> bool:
        """Bind [(id, human description, preferred trigger), ...].

        The description is what the user reads in the desktop's shortcuts
        dialog, so it is a sentence in their language, not an identifier.
        The preferred trigger is in the portal's own syntax
        ("KP_1", "CTRL+ALT+q"); see `x11_trigger` for where those come from.
        """
        if not self.session_handle:
            return False
        addr = portal("GlobalShortcuts")
        payload = []
        for ident, description, trigger in shortcuts:
            opts = {"description": variant("s", description)}
            if trigger:
                opts["preferred_trigger"] = variant("s", trigger)
            payload.append((ident, opts))
        with PortalCall(BUS, "ns_bind") as req:
            try:
                BUS.call(addr, "BindShortcuts", "oa(sa{sv})sa{sv}",
                         (self.session_handle, payload, "",
                          {"handle_token": variant("s", req.token)}),
                         timeout=timeout)
            except DBusError as exc:
                print(f"[portal] BindShortcuts failed: {exc}", file=sys.stderr)
                return False
            code, results = req.wait(timeout)
        if code != 0:
            print(f"[portal] the shortcuts were not bound (code {code})",
                  file=sys.stderr)
            return False
        with self._lock:
            self.bound = _trigger_map(results.get("shortcuts") or [])
        return True

    def current(self) -> dict[str, str]:
        """What the desktop actually bound, id -> trigger."""
        with self._lock:
            return dict(self.bound)


def _trigger_map(shortcuts) -> dict[str, str]:
    """a(sa{sv}) -> {id: trigger_description}."""
    out: dict[str, str] = {}
    for entry in shortcuts or []:
        try:
            ident, props = entry[0], unwrap(entry[1])
        except Exception:
            continue
        out[str(ident)] = str(props.get("trigger_description")
                              or props.get("preferred_trigger") or "")
    return out


# ---------------------------------------------------------------------------
# FileChooser
# ---------------------------------------------------------------------------


def _uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme and parsed.scheme != "file":
        return ""
    return unquote(parsed.path)


def save_file(title: str, default_name: str, folder: str = "",
              filters: list[tuple[str, list[str]]] | None = None,
              timeout: float = 300.0) -> str:
    """A Save As dialog. Returns the chosen path, or "" when cancelled.

    Five minutes, because this is a human deciding where a screenshot goes
    and there is no such thing as a person being too slow at it.
    """
    if not BUS.connect():
        return ""
    addr = portal("FileChooser")
    options = {
        "current_name": variant("s", default_name),
        "modal": variant("b", True),
    }
    if folder:
        # The portal wants a NUL-terminated byte string here, not a str.
        options["current_folder"] = variant("ay", folder.encode() + b"\0")
    if filters:
        # a(sa(us)) - (name, [(kind, pattern)]); kind 0 is a glob.
        options["filters"] = variant(
            "a(sa(us))",
            [(name, [(0, pattern) for pattern in patterns])
             for name, patterns in filters])
    with PortalCall(BUS, "ns_save") as req:
        options["handle_token"] = variant("s", req.token)
        try:
            BUS.call(addr, "SaveFile", "ssa{sv}", ("", title, options),
                     timeout=timeout)
        except DBusError as exc:
            print(f"[portal] SaveFile failed: {exc}", file=sys.stderr)
            return ""
        code, results = req.wait(timeout)
    if code != 0:
        return ""
    uris = results.get("uris") or []
    return _uri_to_path(uris[0]) if uris else ""


def pick_folder(title: str, folder: str = "", timeout: float = 300.0) -> str:
    """A folder picker. Returns the chosen directory, or "" when cancelled."""
    if not BUS.connect():
        return ""
    addr = portal("FileChooser")
    options = {
        "directory": variant("b", True),
        "modal": variant("b", True),
    }
    if folder:
        options["current_folder"] = variant("ay", folder.encode() + b"\0")
    with PortalCall(BUS, "ns_dir") as req:
        options["handle_token"] = variant("s", req.token)
        try:
            BUS.call(addr, "OpenFile", "ssa{sv}", ("", title, options),
                     timeout=timeout)
        except DBusError as exc:
            print(f"[portal] OpenFile failed: {exc}", file=sys.stderr)
            return ""
        code, results = req.wait(timeout)
    if code != 0:
        return ""
    uris = results.get("uris") or []
    return _uri_to_path(uris[0]) if uris else ""


# ---------------------------------------------------------------------------
# Settings - the desktop's light/dark preference
# ---------------------------------------------------------------------------


def color_scheme() -> str:
    """'dark', 'light', or "" when the desktop has no opinion.

    The theme is still the user's own setting in the menu; this is only the
    default for a config that has never been told, so a dark desktop does
    not get a white overlay on first launch.
    """
    if not BUS.connect():
        return ""
    addr = portal("Settings")
    for member, signature, body in (
            ("ReadOne", "ss", ("org.freedesktop.appearance", "color-scheme")),
            ("Read", "ss", ("org.freedesktop.appearance", "color-scheme"))):
        try:
            value = unwrap(BUS.call(addr, member, signature, body, timeout=5.0))
        except DBusError:
            continue
        while isinstance(value, (list, tuple)) and value:
            value = value[0]
        try:
            # 0 no preference, 1 prefer dark, 2 prefer light.
            return {1: "dark", 2: "light"}.get(int(value), "")
        except (TypeError, ValueError):
            return ""
    return ""
