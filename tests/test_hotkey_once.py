"""One press of a hotkey must produce exactly one command.

There are two paths to the same command: RegisterHotKey (WM_HOTKEY) and the
polling fallback that exists for games which grab the keyboard. Both ran for
every press, and nothing told the poller that a WM_HOTKEY had already been
delivered - so on the desktop every hotkey fired TWICE. A toggle looked stuck
("Num1 only ever switches NR on"), the menu opened and closed on one press,
and inside a game it all worked, because a game that swallows WM_HOTKEY leaves
the poller as the only path.

The second half of the same bug: a key held down re-fired the command every
cooldown. A press is an edge, not a state.

F13 is the test key: nothing else on the machine uses it, and no physical
keyboard sends it by accident.

Run:  python3 test_hotkey_once.py
"""
import ctypes
import queue
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

from hotkeys import HotkeyController, MOD_NOREPEAT  # noqa: E402

VK_F13 = 0x7C
KEYEVENTF_KEYUP = 0x0002
COLLECT_S = 1.2


def tap(hold: float = 0.12) -> None:
    """Press and release, holding as long as a person does.

    Not an instant press: while a game that grabs the keyboard has the focus,
    WM_HOTKEY never arrives and the poller - which samples every 30 ms - is
    the only path left. A zero-length synthetic tap falls between two samples
    and is lost, which is a property of the machine, not what this test is
    about. A human press lasts 50-100 ms.
    """
    u = ctypes.windll.user32
    u.keybd_event(VK_F13, 0, 0, 0)
    time.sleep(hold)
    u.keybd_event(VK_F13, 0, KEYEVENTF_KEYUP, 0)


def collect(q: queue.Queue, seconds: float) -> list:
    got = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            got.append(q.get(timeout=0.05))
        except queue.Empty:
            pass
    return got


def main() -> int:
    commands: queue.Queue = queue.Queue()
    bindings = {1: (MOD_NOREPEAT, VK_F13, "toggle", "F13")}
    ctl = HotkeyController(commands, bindings)
    ctl.start()
    if "F13" not in ctl.registered:
        print(f"SKIP: F13 could not be registered (taken by {ctl.failed})")
        ctl.stop()
        return 0

    failures = []
    try:
        # A plain tap.
        tap()
        got = collect(commands, COLLECT_S)
        print(f"one tap        -> {got}")
        if got != ["toggle"]:
            failures.append(f"one tap produced {len(got)} commands: {got}")

        # Held down for longer than the poller's cooldown.
        tap(hold=0.7)
        got = collect(commands, COLLECT_S)
        print(f"held for 0.7 s -> {got}")
        if got != ["toggle"]:
            failures.append(f"holding the key produced {len(got)} commands: {got}")

        # Two deliberate taps are two commands - the fix must not swallow real
        # presses.
        tap()
        time.sleep(0.45)
        tap()
        got = collect(commands, COLLECT_S)
        print(f"two taps       -> {got}")
        if got != ["toggle", "toggle"]:
            failures.append(f"two taps produced {len(got)} commands: {got}")
    finally:
        ctl.stop()

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: one press, one command - and a held key does not repeat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
