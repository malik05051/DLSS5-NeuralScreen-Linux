"""A key pressed during a remap must not fire after the rebind.

The original bug: the user remaps a hotkey, the menu suspends the hotkeys
and captures the key, then rebinds. The key they just pressed was still
physically down when the polling fallback came back, and the poller - whose
key-state table had never seen that key - treated it as a fresh press and
fired the command the user had just reassigned. Remapping Divide to Num2
executed the Num2 action on the spot.

The shape of the bug survives the port even though the mechanism does not.
The portal path cannot have it - the compositor sends an activation for a
press, not a level, and there is no state table to re-baseline. The evdev
fallback can: it reads presses and releases off the device, and a key held
across the suspend produces its release first and its next press second.

So this now checks the evdev watcher, which is the only half where the bug
was ever possible:

  * a key held across suspend+rebind fires nothing;
  * released and pressed again, it fires exactly once, and with the command
    it was rebound to rather than the one it used to carry.

Run:  python3 test_hotkey_remap_no_fire.py
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hotkeys  # noqa: E402

KEY_KP2 = hotkeys.KEY_NUMPAD[2]
KEY_KPSLASH = hotkeys.KEY_KPSLASH

PRESS, RELEASE, REPEAT = 1, 0, 2


def main() -> int:
    failures = []
    fired: list = []
    watcher = hotkeys._EvdevWatcher(fired.append)
    watcher.rebind({1: (hotkeys.MOD_NOREPEAT, KEY_KPSLASH, "screenshot_menu",
                        "Numdiv")})

    # 1. The user presses the key they are about to reassign, with the
    #    hotkeys suspended (which is what the menu does while capturing).
    watcher.suspend()
    watcher._key(KEY_KPSLASH, PRESS)
    if fired:
        failures.append(f"a suspended watcher fired {fired}")

    # 2. The rebind lands while the key is still down.
    watcher.rebind({1: (hotkeys.MOD_NOREPEAT, KEY_KP2, "settings", "Num2")})
    watcher.resume()
    if fired:
        failures.append(f"the rebind itself fired {fired}")

    # 3. The key comes up. A release must never be a command.
    watcher._key(KEY_KPSLASH, RELEASE)
    if fired:
        failures.append(f"a release fired {fired}")

    # 4. The newly bound key, pressed once, fires its new command once.
    watcher._key(KEY_KP2, PRESS)
    if fired != ["settings"]:
        failures.append(f"a fresh press fired {fired}, expected ['settings']")

    # 5. Autorepeat is not a press. Holding Num2 must not open the menu
    #    sixty times a second.
    fired.clear()
    watcher._last.clear()
    for _ in range(10):
        watcher._key(KEY_KP2, REPEAT)
    if fired:
        failures.append(f"autorepeat fired {fired}")

    # 6. The old key is genuinely gone: pressing it does nothing at all.
    fired.clear()
    watcher._last.clear()
    watcher._key(KEY_KPSLASH, PRESS)
    if fired:
        failures.append(f"the unbound key still fired {fired}")

    # 7. The cooldown: two presses inside it are one command. The evdev
    #    path sees the raw device, and a key that bounces is a key that
    #    would toggle NR twice.
    fired.clear()
    watcher._last.clear()
    watcher._key(KEY_KP2, PRESS)
    watcher._key(KEY_KP2, PRESS)
    if fired != ["settings"]:
        failures.append(f"the cooldown let {len(fired)} through, expected 1")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: a key held across a remap fires nothing; the new binding "
          "fires once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
