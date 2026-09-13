"""Hotkey parsing, rebinding and default-binding sanity.

The point of this test is that the hotkey layer does not break when the
default bindings change (they moved from Insert to Num0 in v1.4 and a
measurement script broke because it hard-coded the old key). So:

  * every key name the parser knows round-trips through parse_binding;
  * aliases (CONTROL == CTRL, case, whitespace) resolve to the same keycode;
  * build_bindings applies overrides and silently ignores bad ones;
  * DEFAULT_BINDINGS are internally consistent: unique commands, valid keys;
  * every default binding has a trigger the GlobalShortcuts portal will
    accept - a key we cannot name is a key the desktop silently does not
    bind, which is the one failure mode that looks like the program
    ignoring the keyboard;
  * numlock_needed() is empty, and that is the point: on Windows the
    numpad with Num Lock off sent the navigation keycodes and every numpad
    binding silently did nothing. Wayland reports the keycode, so KP_1 is
    KP_1 either way and there is nothing left to warn about;
  * the test itself never hard-codes a key: it reads DEFAULT_BINDINGS.

Run:  python3 test_hotkey_bindings.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the project root
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

from hotkeys import (DEFAULT_BINDINGS, KEY_NUMPAD, _KEY_NAMES,  # noqa: E402
                     MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT,
                     build_bindings, numlock_needed, parse_binding,
                     trigger_for)


def main() -> int:
    failures = []

    # 1. Every key name the parser knows must parse back to a keycode.
    for name, vk in _KEY_NAMES.items():
        parsed = parse_binding(name)
        if parsed is None:
            failures.append(f"parse_binding({name!r}) returned None")
        elif parsed[1] != vk:
            failures.append(f"parse_binding({name!r}) -> {parsed[1]:#x}, "
                            f"expected {vk:#x}")

    # 2. Aliases and case/whitespace tolerance. The expected codes are read
    #    from the parser's own table rather than written out: they are evdev
    #    keycodes now, not VKs, and a test that hard-codes them is a test
    #    that has to be edited every time the table is - which is exactly
    #    what this file's docstring says not to do.
    for text, expect in (
        ("Ctrl+Alt+Q", (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, _KEY_NAMES["Q"])),
        ("CONTROL+ALT+Q", (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, _KEY_NAMES["Q"])),
        (" ctrl + alt + q ", (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, _KEY_NAMES["Q"])),
        ("Shift+F1", (MOD_SHIFT | MOD_NOREPEAT, _KEY_NAMES["F1"])),
        ("Num0", (MOD_NOREPEAT, KEY_NUMPAD[0])),
        ("Insert", (MOD_NOREPEAT, _KEY_NAMES["INSERT"])),
    ):
        got = parse_binding(text)
        if got != expect:
            failures.append(f"parse_binding({text!r}) -> {got}, expected {expect}")

    # 3. Malformed strings are rejected, not half-parsed.
    for bad in ("", "Ctrl+", "+Q", "Ctrl+Alt+Q+W", "FakeKey", "Ctrl+Q+Alt"):
        if parse_binding(bad) is not None:
            failures.append(f"parse_binding({bad!r}) should be None")

    # 4. build_bindings: overrides apply, bad ones are ignored.
    overrides = {"toggle": "F10", "record": "Insert", "settings": "NotAKey"}
    built = build_bindings(overrides)
    by_cmd = {entry[2]: entry for entry in built.values()}
    if by_cmd["toggle"][1] != _KEY_NAMES["F10"]:
        failures.append("toggle override did not apply (F10)")
    if by_cmd["record"][1] != _KEY_NAMES["INSERT"]:
        failures.append("record override did not apply (Insert)")
    if by_cmd["settings"][1] != DEFAULT_BINDINGS[2][1]:
        failures.append("bad override replaced the default settings binding")

    # 5. DEFAULT_BINDINGS: unique commands, valid keys, sane mods.
    cmds = [entry[2] for entry in DEFAULT_BINDINGS.values()]
    if len(cmds) != len(set(cmds)):
        failures.append("DEFAULT_BINDINGS has duplicate commands")
    for hk_id, (mods, vk, cmd, name) in DEFAULT_BINDINGS.items():
        if vk not in _KEY_NAMES.values():
            failures.append(f"binding {name} has an unknown VK {vk:#x}")
        if not (mods & MOD_NOREPEAT):
            failures.append(f"binding {name} lacks MOD_NOREPEAT")
        if parse_binding(name) is None:
            failures.append(f"binding name {name!r} does not parse")

    # 6. Num Lock is no longer a condition - see the docstring.
    needed = numlock_needed()
    if needed:
        failures.append(f"numlock_needed should be empty on Wayland, "
                        f"got {needed}")

    # 7. Every default binding can be expressed as a portal trigger. An
    #    unnameable key is one the desktop will not bind, and the user sees
    #    a hotkey that does nothing rather than an error.
    for mods, code, cmd, name in DEFAULT_BINDINGS.values():
        trigger = trigger_for(mods, code)
        if not trigger:
            failures.append(f"{name} ({cmd}) has no portal trigger name")
    # The numpad defaults must come out as the KP_ keysyms, because that is
    # what every compositor's shortcut parser understands.
    for digit, code in KEY_NUMPAD.items():
        if trigger_for(MOD_NOREPEAT, code) != f"KP_{digit}":
            failures.append(f"numpad {digit} -> "
                            f"{trigger_for(MOD_NOREPEAT, code)!r}, want KP_{digit}")
    if trigger_for(MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                   _KEY_NAMES["Q"]) != "CTRL+ALT+q":
        failures.append("Ctrl+Alt+Q does not spell as the portal expects")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: {len(_KEY_NAMES)} key names, aliases, overrides, "
          f"{len(DEFAULT_BINDINGS)} default bindings, numlock set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
