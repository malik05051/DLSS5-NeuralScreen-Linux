"""HotkeyController - global hotkeys through the GlobalShortcuts portal,
with an evdev fallback.

RegisterHotKey had a property nothing on Wayland reproduces exactly: the
system delivered the key to us and to nobody else, so Num1 inside a game
toggled NR and the game never saw the press. The portal's GlobalShortcuts
does the same thing - the compositor owns the keyboard and routes the
binding to us - with one difference that matters and one that does not.

The one that matters: **we do not choose the key.** We say which actions
exist, describe them for a human, and name a preferred trigger. The
compositor, or the user in their desktop's settings, decides what the
binding actually is. So `"hotkeys": {"toggle": "Num1"}` in the config is a
preference, and `HotkeyController.effective()` reports what the desktop
really assigned, which is what the menu shows. A program that displayed its
own wish as though it were the truth would be lying to the user the first
time their desktop overrode it.

The one that does not: **Num Lock is no longer a condition.** On Windows the
numpad with Num Lock off sends Insert/End/arrows and nothing can tell them
apart from the dedicated navigation keys, which is why the README had a
paragraph about it and why `numlock_needed()` existed. Wayland reports
keycodes, and KP_1 is KP_1 either way. The function stays, returns nothing,
and says why - call sites keep working and the warning stops appearing.

Where there is no GlobalShortcuts portal (xdg-desktop-portal-wlr ships
none, so plain wlroots compositors and niri have nothing), the fallback
reads /dev/input directly. That needs the user to be in the `input` group,
it sees keys the focused application also gets, and it is off unless the
devices are actually readable - all three of which are stated in the log
rather than discovered.
"""

from __future__ import annotations

import glob
import os
import queue
import select
import struct
import sys
import threading
import time

import portal

# The modifier bits, kept at their Windows values so the parsed tuples that
# travel through the config and the menu do not change shape.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000   # no longer meaningful: the portal never repeats

# Linux input event codes (linux/input-event-codes.h). These take the place
# of the VK_* constants: they are positions on the keyboard, which is what
# both the portal's keysym names and the evdev fallback are derived from.
KEY_ESC = 1
KEY_Q = 16
KEY_F1 = 59
KEY_LEFTCTRL, KEY_RIGHTCTRL = 29, 97
KEY_LEFTALT, KEY_RIGHTALT = 56, 100
KEY_LEFTSHIFT, KEY_RIGHTSHIFT = 42, 54
KEY_NUMLOCK = 69
KEY_INSERT, KEY_DELETE, KEY_HOME, KEY_END = 110, 111, 102, 107
KEY_PAGEUP, KEY_PAGEDOWN = 104, 109
KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 103, 108, 105, 106

#: The numpad, by digit. Unlike the Windows VK_NUMPAD block these are the
#: same codes with Num Lock on or off - the difference is in the keysym the
#: layout produces, not in the key.
KEY_NUMPAD = {0: 82, 1: 79, 2: 80, 3: 81, 4: 75,
              5: 76, 6: 77, 7: 71, 8: 72, 9: 73}
KEY_KPDOT, KEY_KPPLUS, KEY_KPMINUS = 83, 78, 74
KEY_KPASTERISK, KEY_KPSLASH = 55, 98

_MOD_KEYS = {KEY_LEFTCTRL: MOD_CONTROL, KEY_RIGHTCTRL: MOD_CONTROL,
             KEY_LEFTALT: MOD_ALT, KEY_RIGHTALT: MOD_ALT,
             KEY_LEFTSHIFT: MOD_SHIFT, KEY_RIGHTSHIFT: MOD_SHIFT}

# id -> (modifiers, keycode, command, human-readable name)
#
# The defaults stay on the numpad, and the reasoning is the Windows build's,
# minus the part that no longer applies. Function keys keep colliding with
# things - F9 is quickload in Bethesda titles, F8/F7 are screenshots in some
# engines - while the numpad is a block almost nothing competes for and
# 0-1-2-3 in a row is easier to remember than three scattered F-keys.
#
# What is gone from the old reasoning: the Num Lock caveat, and the warning
# that these keys stop working in other programs. The compositor routes a
# bound shortcut to us and, unlike RegisterHotKey, the user can see and
# change every one of them in their own settings.
#
# Quitting stays on Ctrl+Alt+Q: a single key for it is too easy to hit.
DEFAULT_BINDINGS = {
    1: (MOD_NOREPEAT, KEY_NUMPAD[1], "toggle", "Num1"),
    2: (MOD_NOREPEAT, KEY_NUMPAD[2], "settings", "Num2"),
    3: (MOD_NOREPEAT, KEY_NUMPAD[6], "scale_up", "Num6"),
    4: (MOD_NOREPEAT, KEY_NUMPAD[4], "scale_down", "Num4"),
    5: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, KEY_Q, "quit", "Ctrl+Alt+Q"),
    6: (MOD_NOREPEAT, KEY_NUMPAD[0], "record", "Num0"),
    7: (MOD_NOREPEAT, KEY_NUMPAD[3], "screenshot_menu", "Num3"),
    8: (MOD_NOREPEAT, KEY_NUMPAD[5], "window_mode", "Num5"),
}

# Key name -> evdev code (for parsing the config). The same vocabulary the
# Windows build accepted, so a config written there parses here.
_KEY_NAMES = {
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64,
    "F7": 65, "F8": 66, "F9": 67, "F10": 68, "F11": 87, "F12": 88,
    "INSERT": KEY_INSERT, "DELETE": KEY_DELETE, "HOME": KEY_HOME,
    "END": KEY_END, "PGUP": KEY_PAGEUP, "PGDN": KEY_PAGEDOWN,
    "UP": KEY_UP, "DOWN": KEY_DOWN, "LEFT": KEY_LEFT, "RIGHT": KEY_RIGHT,
    "ESC": KEY_ESC, "SPACE": 57, "TAB": 15, "ENTER": 28, "BACKSPACE": 14,
    "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21, "U": 22, "I": 23,
    "O": 24, "P": 25, "A": 30, "S": 31, "D": 32, "F": 33, "G": 34, "H": 35,
    "J": 36, "K": 37, "L": 38, "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48,
    "N": 49, "M": 50,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9,
    "9": 10, "0": 11,
    "NUMDOT": KEY_KPDOT, "NUMPLUS": KEY_KPPLUS, "NUMMINUS": KEY_KPMINUS,
    "NUMMUL": KEY_KPASTERISK, "NUMDIV": KEY_KPSLASH,
}
for _n, _code in KEY_NUMPAD.items():
    _KEY_NAMES[f"NUM{_n}"] = _code

#: evdev code -> the keysym name the portal wants in preferred_trigger.
#: The portal's syntax is xkb keysym names joined by '+', with the modifiers
#: spelled in capitals: "CTRL+ALT+q", "KP_1".
_TRIGGER_NAMES = {code: f"KP_{n}" for n, code in KEY_NUMPAD.items()}
_TRIGGER_NAMES.update({
    KEY_KPDOT: "KP_Decimal", KEY_KPPLUS: "KP_Add", KEY_KPMINUS: "KP_Subtract",
    KEY_KPASTERISK: "KP_Multiply", KEY_KPSLASH: "KP_Divide",
    KEY_INSERT: "Insert", KEY_DELETE: "Delete", KEY_HOME: "Home",
    KEY_END: "End", KEY_PAGEUP: "Prior", KEY_PAGEDOWN: "Next",
    KEY_UP: "Up", KEY_DOWN: "Down", KEY_LEFT: "Left", KEY_RIGHT: "Right",
    KEY_ESC: "Escape", 57: "space", 15: "Tab", 28: "Return", 14: "BackSpace",
})
for _n in range(12):
    _TRIGGER_NAMES[(59 + _n) if _n < 10 else (87 + _n - 10)] = f"F{_n + 1}"
for _name, _code in _KEY_NAMES.items():
    if len(_name) == 1 and _name.isalnum():
        _TRIGGER_NAMES.setdefault(_code, _name.lower())


def parse_binding(text: str) -> tuple[int, int] | None:
    """"Ctrl+Alt+Q" -> (mods, keycode). None when it does not parse."""
    if not text:
        return None
    mods = MOD_NOREPEAT
    raw = [p.strip() for p in str(text).split("+")]
    # An empty part is a malformed binding, not something to skip over:
    # "+Q", "Ctrl+" and "Ctrl++Q" all mean the user's config has a typo,
    # and half-parsing one of them would silently bind a key they did not
    # ask for.
    if not raw or any(not p for p in raw):
        return None
    parts = raw
    for part in parts[:-1]:
        upper = part.upper()
        # CONTROL and CTRL are the same thing; a config that spells it out
        # is not a config with a typo in it.
        if upper in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif upper == "ALT":
            mods |= MOD_ALT
        elif upper == "SHIFT":
            mods |= MOD_SHIFT
        else:
            return None
    code = _KEY_NAMES.get(parts[-1].upper())
    if code is None:
        return None
    return (mods, code)


def build_bindings(overrides: dict | None = None) -> dict:
    """Bindings with the user's overrides from the config applied.

    overrides: {"toggle": "F10", "record": "Insert", ...} — command -> string.
    Unknown or malformed strings are ignored and the default stays.
    """
    bindings = {hk_id: tuple(entry) for hk_id, entry in DEFAULT_BINDINGS.items()}
    if not overrides:
        return bindings
    for hk_id, (mods, code, cmd, name) in list(bindings.items()):
        text = overrides.get(cmd)
        if not text:
            continue
        parsed = parse_binding(text)
        if parsed is None:
            continue
        new_mods, new_code = parsed
        bindings[hk_id] = (new_mods, new_code, cmd, text)
    return bindings


def describe(bindings: dict | None = None) -> str:
    """A line like 'Num1=toggle, Num2=settings, ...' for the startup log."""
    src = bindings or DEFAULT_BINDINGS
    return ", ".join(f"{name}={cmd}"
                     for _, (_, _, cmd, name) in sorted(src.items()))


def trigger_for(mods: int, code: int) -> str:
    """The portal's preferred_trigger syntax for one binding.

    "CTRL+ALT+q", "KP_1". An unknown key returns "" and the portal is left
    to pick something, which is better than sending it a name it will
    reject and losing the whole BindShortcuts call.
    """
    name = _TRIGGER_NAMES.get(code)
    if not name:
        return ""
    parts = []
    if mods & MOD_CONTROL:
        parts.append("CTRL")
    if mods & MOD_ALT:
        parts.append("ALT")
    if mods & MOD_SHIFT:
        parts.append("SHIFT")
    parts.append(name)
    return "+".join(parts)


def numlock_on() -> bool:
    """Always True here, and the reason is worth keeping in the code.

    On Windows the numpad with Num Lock off produced the navigation
    keycodes, indistinguishable from the dedicated keys, so every numpad
    binding silently did nothing and the program had to warn about it.
    Wayland delivers the keycode; the layout decides the keysym; KP_1 and
    Insert are different keys either way. Nothing to check, so nothing to
    warn about.
    """
    return True


def numlock_needed(bindings: dict | None = None) -> list:
    """Bindings that need Num Lock. Always empty - see `numlock_on`."""
    return []


# ---------------------------------------------------------------------------
# The evdev fallback
# ---------------------------------------------------------------------------

#: struct input_event: two longs of timeval, then type, code, value.
_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
_EV_KEY = 0x01

POLL_COOLDOWN = 0.25


def _keyboard_devices() -> list[str]:
    """The /dev/input nodes that look like keyboards and can be read.

    by-path names ending in -kbd are the reliable marker; when udev has not
    written them, every event node is tried and the ones that cannot be
    opened are skipped. Opening a mouse and reading nothing from it costs
    one file descriptor.
    """
    paths = sorted(glob.glob("/dev/input/by-path/*-event-kbd"))
    if not paths:
        paths = sorted(glob.glob("/dev/input/event*"))
    return [p for p in paths if os.access(p, os.R_OK)]


class _EvdevWatcher:
    """Raw key presses, for desktops with no GlobalShortcuts portal.

    Deliberately not python-evdev: the format is five fields and a read
    loop, and the Windows build's whole point about the runtime was that it
    brings only what it needs.

    This sees keys the focused application also sees - it cannot take a key
    away from a game the way the portal can. That is a real difference and
    the log says so when this path is the one in use.
    """

    def __init__(self, on_command):
        self._on_command = on_command
        self._bindings: dict = {}
        self._down: set[int] = set()
        self._mods = 0
        self._last: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._suspended = False
        self.devices: list[str] = []

    def start(self, bindings: dict) -> bool:
        self.devices = _keyboard_devices()
        if not self.devices:
            return False
        self.rebind(bindings)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ns-evdev",
                                        daemon=True)
        self._thread.start()
        return True

    def rebind(self, bindings: dict) -> None:
        with self._lock:
            self._bindings = {(mods & ~MOD_NOREPEAT, code): cmd
                              for _, (mods, code, cmd, _n) in bindings.items()}

    def suspend(self) -> None:
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        files = []
        for path in self.devices:
            try:
                files.append(open(path, "rb", buffering=0))
            except OSError:
                continue
        if not files:
            return
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select(files, [], [], 0.2)
                for handle in ready:
                    try:
                        data = handle.read(_EVENT_SIZE)
                    except OSError:
                        continue
                    if not data or len(data) < _EVENT_SIZE:
                        continue
                    _s, _us, kind, code, value = struct.unpack(_EVENT_FMT, data)
                    if kind != _EV_KEY:
                        continue
                    self._key(code, value)
        finally:
            for handle in files:
                try:
                    handle.close()
                except OSError:
                    pass

    def _key(self, code: int, value: int) -> None:
        # value: 0 release, 1 press, 2 autorepeat. Autorepeat is dropped -
        # holding Num1 must not toggle NR sixty times.
        if code in _MOD_KEYS:
            if value:
                self._mods |= _MOD_KEYS[code]
            else:
                self._mods &= ~_MOD_KEYS[code]
            return
        if value != 1 or self._suspended:
            return
        with self._lock:
            command = self._bindings.get((self._mods, code))
        if command is None:
            return
        now = time.monotonic()
        if now - self._last.get(command, 0.0) < POLL_COOLDOWN:
            return
        self._last[command] = now
        self._on_command(command)


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------


class HotkeyController:
    """Binds the global hotkeys; commands go into a queue.

    The same contract the Windows version had - `start`, `stop`, `suspend`,
    `resume`, `rebind`, and `registered`/`failed` for the startup log - so
    main.py's loop and the menu's rebinding page did not have to change.
    """

    def __init__(self, commands: queue.Queue, bindings: dict | None = None):
        self._commands = commands
        self._bindings = bindings or DEFAULT_BINDINGS
        self.registered: list[str] = []
        self.failed: list[str] = []
        self.backend = ""          # "portal", "evdev" or ""
        self._session: portal.ShortcutsSession | None = None
        self._evdev: _EvdevWatcher | None = None
        self._suspended = False
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self, labels: dict | None = None) -> None:
        """Bind everything. Never raises: no hotkeys is a degraded program,
        not a stopped one - the tray and the menu still work."""
        if self._bind_portal(labels):
            self.backend = "portal"
            return
        self._evdev = _EvdevWatcher(self._fire)
        if self._evdev.start(self._bindings):
            self.backend = "evdev"
            self.registered = [name for _, (_, _, _c, name)
                               in sorted(self._bindings.items())]
            print(f"[hotkeys] no GlobalShortcuts portal; reading "
                  f"{len(self._evdev.devices)} keyboard device(s) directly. "
                  "The keys still reach the focused application, which the "
                  "portal path would have prevented.", file=sys.stderr)
            return
        self._evdev = None
        self.failed = [name for _, (_, _, _c, name)
                       in sorted(self._bindings.items())]
        print("[hotkeys] no global hotkeys: this desktop has no "
              "GlobalShortcuts portal and /dev/input is not readable (add "
              "your user to the 'input' group for the fallback). The menu is "
              "still reachable from the tray icon.", file=sys.stderr)

    def _bind_portal(self, labels: dict | None) -> bool:
        session = portal.ShortcutsSession(self._fire)
        if not session.open():
            return False
        labels = labels or {}
        shortcuts = []
        for _id, (mods, code, cmd, name) in sorted(self._bindings.items()):
            shortcuts.append((cmd, labels.get(cmd) or cmd,
                              trigger_for(mods, code)))
        if not session.bind(shortcuts):
            session.close()
            return False
        self._session = session
        bound = session.current()
        self.registered = [f"{cmd}={bound.get(cmd) or 'unbound'}"
                           for cmd, _d, _t in shortcuts]
        self.failed = [cmd for cmd, _d, _t in shortcuts if not bound.get(cmd)]
        return True

    def _fire(self, command: str) -> None:
        if self._suspended:
            return
        self._commands.put(command)

    # -- what main.py calls ------------------------------------------------

    def suspend(self) -> None:
        """Stop delivering while the menu is capturing a key to rebind.

        The portal has no "pause": a bound shortcut keeps firing. So the
        suspension is on our side of the queue, which is enough - the point
        is that pressing Num1 to assign it must not also toggle NR.
        """
        self._suspended = True
        if self._evdev is not None:
            self._evdev.suspend()

    def resume(self) -> None:
        self._suspended = False
        if self._evdev is not None:
            self._evdev.resume()

    def rebind(self, bindings: dict, labels: dict | None = None) -> None:
        """Apply a new set of bindings.

        On the portal path this is another BindShortcuts, which on most
        desktops shows the shortcuts dialog again - the user is changing a
        shortcut, so being shown the shortcut editor is not a surprise.
        """
        with self._lock:
            self._bindings = bindings
            if self._evdev is not None:
                self._evdev.rebind(bindings)
                return
            if self._session is not None:
                labels = labels or {}
                shortcuts = [(cmd, labels.get(cmd) or cmd,
                              trigger_for(mods, code))
                             for _id, (mods, code, cmd, _n)
                             in sorted(bindings.items())]
                if self._session.bind(shortcuts):
                    bound = self._session.current()
                    self.registered = [f"{cmd}={bound.get(cmd) or 'unbound'}"
                                       for cmd, _d, _t in shortcuts]

    def effective(self) -> dict:
        """command -> the trigger the desktop actually assigned.

        Empty on the evdev path, where our own binding is the truth. The
        menu falls back to the configured name when a command is missing
        here, which is also what happens before the portal has answered.
        """
        if self._session is not None:
            return self._session.current()
        return {}

    def stop(self) -> None:
        if self._evdev is not None:
            self._evdev.stop()
            self._evdev = None
        if self._session is not None:
            self._session.close()
            self._session = None
