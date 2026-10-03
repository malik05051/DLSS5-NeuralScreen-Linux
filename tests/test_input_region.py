"""The overlay takes clicks where the menu is, and nowhere else.

This is the one that keeps going wrong. The overlay is the size of the
desktop, so the input region is the whole difference between "a menu you
can click" and "a desktop that has stopped responding", and four separate
bugs have lived in it: events never arriving, nobody reading them, the
region covering the whole screen while the menu was open, and the region
covering the whole screen for as long as the menu stayed open because the
panel's geometry was not known when click-through came off.

What is checked here is the region arithmetic alone - which rectangles the
surface is told to accept input in, for each state the menu can be in.
That needs no compositor: wl_surface.set_input_region is recorded by a
stand-in surface and the rectangles are read back.

The rule the cases encode: an empty region is the safe failure. A click
that falls through to the desktop costs the user one click; a region that
covers the screen costs them their session.

Run:  python3 test_input_region.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wayland_shell  # noqa: E402

W, H = 2560, 1440
PANEL = (900, 400, 520, 640)


class _Region:
    """wl_region: remembers the rectangles added to it."""

    def __init__(self, sink):
        self._sink = sink
        self.rects = []

    def add(self, x, y, w, h):
        self.rects.append((x, y, w, h))

    def destroy(self):
        self._sink.last_region = list(self.rects)


class _Compositor:
    def __init__(self, sink):
        self._sink = sink

    def create_region(self):
        return _Region(self._sink)


class _Surface:
    def __init__(self, sink):
        self._sink = sink

    def set_input_region(self, region):
        self._sink.applied = list(region.rects)

    def commit(self):
        pass


class _Shell:
    def __init__(self, sink):
        self.compositor = _Compositor(sink)


class _Overlay(wayland_shell.Overlay):
    """The real input-region logic on a stand-in surface.

    Overlay.__init__ talks to a compositor, so the state the region
    arithmetic reads is set up directly. If a field here stops matching the
    real __init__ the test is wrong rather than passing by luck, which is
    what the guard below checks.
    """

    def __init__(self):
        self.applied = []
        self.last_region = []
        self._shell = _Shell(self)
        self.surface = _Surface(self)
        self.width, self.height = W, H
        self._click_through = True
        self._input_rects = []
        self._input_all = False


def main() -> int:
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: expected {want}, got {got}")

    # The fields the test sets up are the ones the real Overlay has.
    import inspect
    src = inspect.getsource(wayland_shell.Overlay.__init__)
    for field in ("_input_rects", "_input_all", "_click_through"):
        if field not in src:
            failures.append(f"Overlay.__init__ no longer sets {field} - "
                            "this test's stand-in is out of date")

    # 1. Menu closed: click-through, so nothing is accepted whatever the
    #    rectangles say. This is the state the program spends most of its
    #    life in.
    ov = _Overlay()
    ov.set_input_rects([PANEL])
    check("closed menu accepts nothing", ov.applied, [])

    # 2. Menu open over the panel: exactly the panel, and nothing else. A
    #    click anywhere else on the screen has to reach the desktop.
    ov = _Overlay()
    ov.set_input_rects([PANEL])
    ov.set_click_through(False)
    check("open menu accepts the panel only", ov.applied, [PANEL])

    # 3. The regression. No rectangle known yet - panel_rect is 0x0 until
    #    the menu's first draw - and click-through comes off. The region
    #    must be empty, NOT the whole surface: "nowhere" is not
    #    "everywhere". This is what froze the desktop.
    ov = _Overlay()
    ov.set_input_rects([])
    ov.set_click_through(False)
    check("open menu with no geometry accepts nothing", ov.applied, [])

    # 4. Same thing by the other route: a zero-sized rectangle is filtered
    #    out, and must not promote the region to the whole surface.
    ov = _Overlay()
    ov.set_click_through(False)
    ov.set_input_rects([(900, 400, 0, 0)])
    check("a zero-sized panel accepts nothing", ov.applied, [])

    # 5. A drag asks for the whole surface by name, because the pointer
    #    leaves the panel and the compositor stops sending motion outside
    #    the region. This is the one case that may cover the screen.
    ov = _Overlay()
    ov.set_click_through(False)
    ov.set_input_rects(None)
    check("a drag accepts the whole surface", ov.applied, [(0, 0, W, H)])

    # 6. And it gives it back: the drag ends, the panel is the region again.
    ov.set_input_rects([PANEL])
    check("after a drag, the panel only", ov.applied, [PANEL])

    # 7. Click-through on top of a drag still wins - the menu closing
    #    mid-drag must not leave the screen captured.
    ov = _Overlay()
    ov.set_click_through(False)
    ov.set_input_rects(None)
    ov.set_click_through(True)
    check("click-through overrides a drag", ov.applied, [])

    for line in failures:
        print(f"FAIL: {line}")
    if failures:
        return 1
    print("OK: the input region is the panel, empty, or explicitly the whole "
          "surface for a drag")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
