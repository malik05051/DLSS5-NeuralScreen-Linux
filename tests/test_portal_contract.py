"""The portal calls are shaped the way xdg-desktop-portal expects.

Every capability this program used to take for itself now comes from one
D-Bus service, and the two mistakes that make a portal call hang forever
rather than fail are both checkable offline:

  * **The request handle is predicted, not received.** The portal answers a
    method call with a handle and delivers the result later as a signal on
    that path. The path is built from our unique bus name with the
    punctuation replaced - get that wrong and the subscription is on a path
    nothing is ever emitted on, so the call waits out its timeout and
    reports nothing rather than failing.

  * **The subscription happens before the call.** A fast portal answers
    before a subscription made afterwards would exist.

Also checked: the variant unwrapper, which is what stands between the rest
of the program and `results["streams"][0][1][1]["size"][1]`.

No session bus is needed - and that is deliberate. A test that needed a
desktop to run is a test that does not run in CI.

Run:  python3 test_portal_contract.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dbusio  # noqa: E402
import hotkeys  # noqa: E402


class _FakeBus:
    """A Bus with a unique name and nothing behind it."""

    def __init__(self, unique):
        self._unique = unique
        self.matches = []

    def token_base(self):
        return dbusio._UNIQUE_RE.sub("_", self._unique.lstrip(":"))

    def add_match(self, rule, handler):
        self.matches.append(("add", rule, handler))

    def remove_match(self, rule, handler):
        self.matches.append(("remove", rule, handler))


def main() -> int:
    failures = []

    # 1. The request path. ":1.42" -> "1_42", and the handle is
    #    /org/freedesktop/portal/desktop/request/<sender>/<token>.
    bus = _FakeBus(":1.42")
    call = dbusio.PortalCall(bus, "ns_test")
    want_prefix = "/org/freedesktop/portal/desktop/request/1_42/ns_test"
    if not call.path.startswith(want_prefix):
        failures.append(f"request path {call.path!r} does not start with "
                        f"{want_prefix!r} - the Response signal would never "
                        "reach us")
    if ":" in call.path or "." in call.path.split("/")[-2]:
        failures.append(f"the sender part of {call.path!r} still has "
                        "punctuation the portal strips")

    # 2. The subscription is in place before the body of the `with` runs,
    #    and removed after.
    with call:
        if not any(kind == "add" for kind, _r, _h in bus.matches):
            failures.append("entering PortalCall did not subscribe - a fast "
                            "portal would answer into nothing")
    if not any(kind == "remove" for kind, _r, _h in bus.matches):
        failures.append("leaving PortalCall did not unsubscribe")

    # 3. A timed-out wait reports failure rather than hanging or raising.
    code, results = call.wait(0.01)
    if code == 0 or results != {}:
        failures.append(f"a timed-out wait returned {(code, results)}, "
                        "expected a failure code and no results")

    # 4. The variant unwrapper, on a reply shaped like ScreenCast's.
    reply = {
        "streams": ("a(ua{sv})", [(42, {"size": ("(ii)", (3840, 2160)),
                                        "position": ("(ii)", (0, 0)),
                                        "source_type": ("u", 1)})]),
        "restore_token": ("s", "abc123"),
    }
    plain = dbusio.unwrap(reply)
    try:
        node, props = plain["streams"][0]
        ok = (node == 42 and props["size"] == (3840, 2160)
              and props["source_type"] == 1
              and plain["restore_token"] == "abc123")
    except Exception as exc:
        ok = False
        failures.append(f"unwrap produced something unreadable: {exc}")
    if not ok:
        failures.append(f"unwrap did not flatten the reply: {plain}")

    # 5. A plain 2-tuple of strings is a struct, not a variant. The
    #    heuristic has to leave it alone or a window title starting with a
    #    signature-shaped word would be eaten.
    kept = dbusio.unwrap(("Chrome", "a document"))
    if kept != ("Chrome", "a document"):
        failures.append(f"unwrap ate an ordinary string pair: {kept}")

    # 6. Every default hotkey has a trigger in the portal's own syntax.
    #    An unnameable key is one the desktop will not bind.
    for mods, code_, cmd, name in hotkeys.DEFAULT_BINDINGS.values():
        if not hotkeys.trigger_for(mods, code_):
            failures.append(f"{name} ({cmd}) has no preferred_trigger")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: request paths, subscribe-then-call, unwrap, hotkey triggers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
