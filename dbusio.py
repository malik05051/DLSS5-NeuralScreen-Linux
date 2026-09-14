"""A small synchronous D-Bus client, on jeepney.

Everything this program needs from the desktop on Wayland arrives over the
session bus: the screen capture, the global shortcuts, the file chooser, the
tray icon, the autostart permission. On Windows each of those was a
different API in a different DLL; here they are one transport, so there is
one module that knows how to talk on it and the rest of the program only
knows what it wants.

jeepney rather than dbus-python: it is pure Python and speaks the wire
protocol itself, so the program keeps the property the Windows build was
proud of - unpack the archive and run, no system libraries to match, no
GObject introspection typelibs to have installed.

Two things this wraps that are easy to get wrong:

  * **Signals are not requests.** The portal answers a method call
    immediately with a handle and delivers the actual result later as a
    Response signal on that handle. `PortalCall` does the whole dance:
    predicts the handle, subscribes BEFORE calling (a fast portal answers
    before a subscription made afterwards would exist), then waits.
  * **File descriptors travel out of band.** A PipeWire remote and a
    screenshot file arrive as a unix_fd index into the message's fd array,
    not as a number in the body. `fd_arg` unwraps them.
"""
from __future__ import annotations

import os
import sys
import re
import threading
import time
from typing import Any, Callable

from jeepney import (DBusAddress, MessageType, HeaderFields, MatchRule,
                     new_method_call, message_bus)
from jeepney.io.blocking import open_dbus_connection


#: The portal lives at one well-known name; every interface is an object on
#: the same path.
PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"

_UNIQUE_RE = re.compile(r"[^A-Za-z0-9_]")


class DBusError(RuntimeError):
    """A method call came back as an error, or nothing came back at all."""


class Bus:
    """One session-bus connection, shared by everything that needs the bus.

    Not a connection per feature: the portal identifies an application by
    its bus name, and a tray icon, a shortcuts session and a screen cast
    coming from three different names look like three different programs to
    the desktop - the shortcuts dialog would name us three times.

    Calls are serialised under a lock. Nothing here is on the frame path:
    the capture negotiates once and then reads PipeWire, the shortcuts
    arrive as signals, the file chooser is a human pressing a button.
    """

    def __init__(self):
        self._conn = None
        self._lock = threading.RLock()
        self._unique = ""
        self._router_stop = threading.Event()
        self._handlers: list[tuple[MatchRule, Callable]] = []
        self._router: threading.Thread | None = None
        self._inbox: dict[int, Any] = {}
        self._inbox_event = threading.Condition()

    # -- connection --------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def connect(self) -> bool:
        """Open the session bus. False when there is none (a tty, a service).

        Never raises: every caller has a degraded path, and a program that
        refuses to start because the tray is unreachable is worse than one
        that starts without a tray.
        """
        with self._lock:
            if self._conn is not None:
                return True
            try:
                # enable_fds: the ScreenCast portal returns the PipeWire
                # remote as a file descriptor, and a connection that cannot
                # receive one gets the reply body with nothing behind it.
                try:
                    self._conn = open_dbus_connection(bus="SESSION",
                                                      enable_fds=True)
                except Exception:
                    self._conn = open_dbus_connection(bus="SESSION")
                self._unique = self._conn.unique_name or ""
            except Exception as exc:
                print(f"[dbus] no session bus: {exc}", file=sys.stderr)
                self._conn = None
                return False
            self._router_stop.clear()
            self._router = threading.Thread(target=self._route, name="ns-dbus",
                                            daemon=True)
            self._router.start()
            return True

    @property
    def unique_name(self) -> str:
        return self._unique

    def token_base(self) -> str:
        """The unique bus name with the punctuation the portal forbids gone.

        Request handles are built as
        /org/freedesktop/portal/desktop/request/SENDER/TOKEN where SENDER is
        our unique name minus the leading colon and with dots turned into
        underscores. Getting this wrong means subscribing to a path the
        portal never signals on, and the call simply hangs.
        """
        return _UNIQUE_RE.sub("_", self._unique.lstrip(":"))

    def close(self) -> None:
        self._router_stop.set()
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # -- the router thread -------------------------------------------------

    def _route(self) -> None:
        """Read messages forever, hand replies to waiters and signals to
        handlers.

        One reader: two threads calling receive() on the same connection
        race for each other's replies, which shows up as a call that returns
        somebody else's answer - the kind of bug that appears once a week.
        """
        while not self._router_stop.is_set():
            conn = self._conn
            if conn is None:
                return
            try:
                msg = conn.receive(timeout=0.5)
            except TimeoutError:
                continue
            except Exception:
                if not self._router_stop.is_set():
                    print("[dbus] the connection is gone", file=sys.stderr)
                return
            if msg is None:
                continue
            kind = msg.header.message_type
            if kind in (MessageType.method_return, MessageType.error):
                serial = msg.header.fields.get(HeaderFields.reply_serial)
                with self._inbox_event:
                    self._inbox[serial] = msg
                    self._inbox_event.notify_all()
            elif kind in (MessageType.signal, MessageType.method_call):
                # Method calls come in too: the tray icon is a D-Bus object
                # the panel calls into, not only a client that calls out.
                for rule, handler in list(self._handlers):
                    if rule.matches(msg):
                        try:
                            handler(msg)
                        except Exception as exc:
                            print(f"[dbus] handler failed: {exc}",
                                  file=sys.stderr)

    # -- calls and signals -------------------------------------------------

    def call(self, addr: DBusAddress, member: str, signature: str = "",
             body: tuple = (), timeout: float = 25.0):
        """A blocking method call. Returns the reply body, raises DBusError.

        The timeout is generous because some of these calls put a dialog in
        front of the user - picking a window to capture is not a
        millisecond operation.
        """
        conn = self._conn
        if conn is None:
            raise DBusError("not connected to the session bus")
        msg = new_method_call(addr, member, signature or None,
                              body if signature else ())
        with self._lock:
            # The serial is allocated here rather than left to send(): it is
            # the only thing that ties a reply back to this call, and send()
            # keeps it to itself.
            serial = next(conn.outgoing_serial)
            conn.send(msg, serial=serial)
        deadline = time.monotonic() + timeout
        with self._inbox_event:
            while serial not in self._inbox:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise DBusError(f"{member}: no reply in {timeout:g}s")
                self._inbox_event.wait(min(left, 0.25))
            reply = self._inbox.pop(serial)
        if reply.header.message_type == MessageType.error:
            name = reply.header.fields.get(HeaderFields.error_name)
            detail = reply.body[0] if reply.body else ""
            raise DBusError(f"{member}: {name}: {detail}")
        return reply.body

    def add_match(self, rule: MatchRule, handler: Callable) -> None:
        """Route matching messages to `handler`, on the router thread.

        Signals need an AddMatch on the bus to be delivered at all; method
        calls addressed to us arrive regardless, so the AddMatch for those
        is a no-op the bus accepts and ignores. Registering both the same
        way keeps one code path.
        """
        self._handlers.append((rule, handler))
        if rule.message_type == MessageType.method_call:
            return
        try:
            self.call(message_bus, "AddMatch", "s", (rule.serialise(),))
        except DBusError as exc:
            print(f"[dbus] AddMatch refused: {exc}", file=sys.stderr)

    def remove_match(self, rule: MatchRule, handler: Callable) -> None:
        try:
            self._handlers.remove((rule, handler))
        except ValueError:
            pass
        if rule.message_type == MessageType.method_call:
            return
        try:
            self.call(message_bus, "RemoveMatch", "s", (rule.serialise(),))
        except DBusError:
            pass

    def request_name(self, name: str) -> bool:
        """Take a well-known bus name (the tray needs one). False if taken."""
        try:
            # 0x4 = DBUS_NAME_FLAG_DO_NOT_QUEUE: a second instance must fail
            # loudly rather than sit in a queue and take the name over when
            # the first one exits.
            reply = self.call(message_bus, "RequestName", "su", (name, 0x4))
        except DBusError as exc:
            print(f"[dbus] could not take {name}: {exc}", file=sys.stderr)
            return False
        return bool(reply) and reply[0] in (1, 4)  # PRIMARY_OWNER / ALREADY_OWNER

    def name_has_owner(self, name: str) -> bool:
        try:
            return bool(self.call(message_bus, "NameHasOwner", "s", (name,))[0])
        except DBusError:
            return False


#: The process-wide bus. Modules import this rather than opening their own.
BUS = Bus()


def portal(interface: str) -> DBusAddress:
    """The address of one portal interface, e.g. 'ScreenCast'."""
    return DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS,
                       interface=f"org.freedesktop.portal.{interface}")


class PortalCall:
    """One request/Response round trip with xdg-desktop-portal.

    Used as a context manager so the subscription is in place before the
    method call goes out:

        with PortalCall(bus, "ns_pick") as req:
            bus.call(addr, "SelectSources", "oa{sv}", (session, opts))
            code, results = req.wait(120)

    `code` is 0 for success, 1 for the user cancelling, 2 for anything
    else. `results` is the portal's a{sv}, already unwrapped to plain
    Python.
    """

    _counter = 0
    _counter_lock = threading.Lock()

    def __init__(self, bus: Bus, prefix: str = "ns"):
        self._bus = bus
        with PortalCall._counter_lock:
            PortalCall._counter += 1
            serial = PortalCall._counter
        self.token = f"{prefix}_{os.getpid()}_{serial}"
        self.path = (f"/org/freedesktop/portal/desktop/request/"
                     f"{bus.token_base()}/{self.token}")
        self._rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                               member="Response", path=self.path)
        self._done = threading.Event()
        self._result: tuple[int, dict] = (2, {})

    def _on_signal(self, msg) -> None:
        try:
            code, results = msg.body
        except Exception:
            code, results = 2, {}
        self._result = (int(code), unwrap(results))
        self._done.set()

    def __enter__(self) -> "PortalCall":
        self._bus.add_match(self._rule, self._on_signal)
        return self

    def __exit__(self, *exc) -> None:
        self._bus.remove_match(self._rule, self._on_signal)

    def wait(self, timeout: float = 60.0) -> tuple[int, dict]:
        """Block for the Response signal. (2, {}) when it never comes."""
        if not self._done.wait(timeout):
            return (2, {})
        return self._result


def unwrap(value):
    """Strip jeepney's (signature, value) variant tuples, recursively.

    The portal returns everything as a{sv}, so without this every read is
    `results["streams"][0][1][1]["size"][1]` and a change of one signature
    breaks it silently.
    """
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, tuple):
        # A variant is exactly (signature_string, payload).
        if len(value) == 2 and isinstance(value[0], str) and _is_signature(value[0]):
            return unwrap(value[1])
        return tuple(unwrap(v) for v in value)
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    return value


_SIGNATURE_CHARS = set("ybnqiuxtdsogavre{}() h")


def _is_signature(text: str) -> bool:
    """Does this string look like a D-Bus type signature?

    A heuristic, and it has to be: a variant is a 2-tuple whose first item
    is a signature, and an ordinary struct of (string, something) has the
    same shape. Signatures are short and drawn from a 20-character
    alphabet, so real strings almost never collide - and the ones that
    could ("as", "a") are not values this program's portals return.
    """
    return bool(text) and len(text) <= 16 and set(text) <= _SIGNATURE_CHARS


def variant(signature: str, value):
    """Build a variant for an a{sv} argument."""
    return (signature, value)


def fd_arg(msg, index: int) -> int:
    """A file descriptor out of a reply, by its index in the message.

    jeepney hands file descriptors over as `FileDescriptor` objects when the
    connection was opened with fd passing, and as bare ints otherwise.
    Callers want an int they can pass to a library, and they own it from
    here - closing it is theirs.
    """
    value = msg[index] if isinstance(msg, (list, tuple)) else msg
    to_int = getattr(value, "to_raw_fd", None)
    if to_int is not None:
        return int(to_int())
    fileno = getattr(value, "fileno", None)
    if fileno is not None:
        return os.dup(int(fileno()))
    return int(value)
