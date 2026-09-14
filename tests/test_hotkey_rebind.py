"""suspend / resume / rebind on the hotkey controller.

The Windows version of this test probed the system: it asked whether
Ctrl+Alt+F6 could be registered from another thread, because RegisterHotKey
with hWnd=None is bound to the calling thread and the whole point was that
assignments were being installed and removed from inside the hotkey thread.

None of that exists here and the probe has no counterpart - a client cannot
ask the compositor whether a key is free, and it should not be able to. So
the test moved to the layer that is actually ours: the controller's own
behaviour between the backend and the command queue.

  * suspend stops delivery and resume restores it - which is what the menu
    needs while it is capturing a key, so that pressing Num1 to assign it
    does not also toggle NR;
  * a rebind replaces the bindings the backend is working from, so the key
    the user just reassigned fires the new command and not the old one;
  * `effective()` reports what the desktop bound rather than what we asked
    for, because the compositor and the user have the final say and a menu
    showing our wish would be lying.

A fake portal session stands in for the desktop: no session bus, no
compositor, and the test runs in CI.

Run:  python3 test_hotkey_rebind.py
"""
import os
import queue
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hotkeys  # noqa: E402


class FakeSession:
    """A GlobalShortcuts session that binds whatever it is asked for.

    `assigned` is what the desktop decided, which is deliberately not what
    was requested for one of the shortcuts: that is the case the menu has to
    render honestly.
    """

    def __init__(self, on_activated):
        self.on_activated = on_activated
        self.bound: list = []
        self.assigned: dict = {}
        self.closed = False

    def open(self):
        return True

    def bind(self, shortcuts, timeout=0):
        self.bound = list(shortcuts)
        self.assigned = {ident: trigger for ident, _d, trigger in shortcuts}
        # The desktop refuses one and substitutes another, which is exactly
        # what a compositor with a conflicting binding does.
        if "settings" in self.assigned:
            self.assigned["settings"] = "SUPER+m"
        return True

    def current(self):
        return dict(self.assigned)

    def close(self):
        self.closed = True


def main() -> int:
    failures = []
    commands: queue.Queue = queue.Queue()
    made: list = []

    real_session = hotkeys.portal.ShortcutsSession
    hotkeys.portal.ShortcutsSession = lambda cb: made.append(
        FakeSession(cb)) or made[-1]
    try:
        bindings = hotkeys.build_bindings()
        hk = hotkeys.HotkeyController(commands, bindings)
        hk.start(labels={"toggle": "Neural Rendering on/off"})

        if hk.backend != "portal":
            failures.append(f"backend is {hk.backend!r}, expected 'portal'")
        session = made[-1]

        # 1. The descriptions reach the desktop's shortcut editor: that is
        #    what the user reads there, so it has to be the localised name
        #    and not the internal command id.
        described = dict((ident, text) for ident, text, _t in session.bound)
        if described.get("toggle") != "Neural Rendering on/off":
            failures.append(f"the description sent for 'toggle' was "
                            f"{described.get('toggle')!r}")

        # 2. The preferred trigger is in the portal's own syntax.
        triggers = {ident: trigger for ident, _d, trigger in session.bound}
        if triggers.get("toggle") != "KP_1":
            failures.append(f"toggle's preferred trigger is "
                            f"{triggers.get('toggle')!r}, expected 'KP_1'")
        if triggers.get("quit") != "CTRL+ALT+q":
            failures.append(f"quit's preferred trigger is "
                            f"{triggers.get('quit')!r}, expected 'CTRL+ALT+q'")

        # 3. effective() reports the desktop's answer, not ours.
        if hk.effective().get("settings") != "SUPER+m":
            failures.append("effective() does not report what the desktop "
                            "actually bound - the menu would show our wish")

        # 4. An activation reaches the queue.
        session.on_activated("toggle")
        if commands.get_nowait() != "toggle":
            failures.append("an activation did not reach the command queue")

        # 5. Suspended, it does not.
        hk.suspend()
        session.on_activated("toggle")
        if not commands.empty():
            failures.append("a suspended controller still delivered a command")

        # 6. Resumed, it does again.
        hk.resume()
        session.on_activated("toggle")
        if commands.get_nowait() != "toggle":
            failures.append("resume did not restore delivery")

        # 7. A rebind reaches the backend with the new trigger.
        rebound = hotkeys.build_bindings({"toggle": "F10"})
        hk.rebind(rebound, labels={"toggle": "Neural Rendering on/off"})
        triggers = {ident: trigger for ident, _d, trigger in session.bound}
        if triggers.get("toggle") != "F10":
            failures.append(f"after the rebind toggle's trigger is "
                            f"{triggers.get('toggle')!r}, expected 'F10'")

        # 8. Stopping closes the session: a shortcuts session left open is a
        #    program the desktop still thinks owns those keys.
        hk.stop()
        if not session.closed:
            failures.append("stop() left the shortcuts session open")
    finally:
        hotkeys.portal.ShortcutsSession = real_session

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: descriptions, triggers, suspend/resume, rebind, honest "
          "effective()")
    return 0


if __name__ == "__main__":
    sys.exit(main())
