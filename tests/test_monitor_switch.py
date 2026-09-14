"""A stale monitor must never take the program down (issues #24/#26).

The Windows shape of this bug: the dxcam factory cached its output list at
import, a monitor unplugged or a dock changed after that left stale indices,
and dxcam.create(output_idx=N) raised IndexError for them - "list index out
of range" in the user's log, on launch.

The Linux shape is the same bug with different nouns. The output list is
cached for the same reason (the menu asks on every frame it is open, and a
Wayland roundtrip per frame costs more than the network does), and it goes
stale for exactly the same reasons: a cable, a dock, a display reorder. What
changed is the recovery, and it is better: the config remembers the
connector name ("DP-1"), which survives a reorder, and when the name really
is gone the portal asks the user which screen to use instead of the program
guessing.

Checked:
  * an out-of-range index re-enumerates and falls back to 0 rather than
    raising;
  * a connector name that no longer exists is not fatal - it drops the
    restore token, so the portal shows its picker instead of silently
    restoring a monitor that is not there;
  * a name that does exist resolves to its index and keeps the token, which
    is what makes the second launch silent;
  * the resolution comes back with the output's rotation applied. The
    Windows build could not do this at all: 90 and 270 came out with the
    sides swapped, and it is a known limitation there.

Run:  python3 test_monitor_switch.py
"""
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture  # noqa: E402
import portal  # noqa: E402
from capture import ScreenCapture  # noqa: E402


def fake_output(name, w=1920, h=1080, x=0, y=0, transform=0):
    """An Output as wayland_shell reports one, with nothing behind it."""
    return types.SimpleNamespace(
        name=name, description=f"{name} display", width=w, height=h,
        logical_w=w, logical_h=h, x=x, y=y, scale=1, transform=transform,
        refresh=60000, rotated=transform in (1, 3, 5, 7), proxy=None)


class _FakeSession:
    """A granted ScreenCast session, without a portal."""

    def __init__(self, node=7, size=(1920, 1080), token="tok"):
        self.node_id = node
        self.fd = 99
        self.size = size
        self.position = (0, 0)
        self.restore_token = token
        self.source_type = 1
        self.mapping_id = ""
        self.session_handle = "/fake"
        self.closed = False

    @property
    def ok(self):
        return True

    def close(self):
        self.closed = True


def main() -> int:
    failures = []
    real_outputs = capture._outputs
    real_refresh = capture.refresh_outputs
    real_open = portal.open_screencast
    real_gst = ScreenCapture._start_gstreamer

    outputs = [fake_output("DP-1", 3840, 2160),
               fake_output("HDMI-A-1", 2560, 1440, x=3840)]
    events = []
    asked = []

    capture._outputs = lambda: outputs
    capture.refresh_outputs = lambda: events.append("refresh")
    portal.open_screencast = lambda **kw: (asked.append(kw)
                                           or _FakeSession())
    # The GStreamer fallback path is not what this test is about, and
    # starting it would need a real PipeWire remote behind the fake fd.
    ScreenCapture._start_gstreamer = lambda self: None

    try:
        # 1. An index past the end of the list: re-enumerate, fall back to 0.
        asked.clear()
        events.clear()
        cap = ScreenCapture(monitor_idx=5)
        if "refresh" not in events:
            failures.append("an out-of-range index did not re-enumerate")
        if cap.monitor_idx != 0:
            failures.append(f"out-of-range index did not fall back: "
                            f"monitor_idx={cap.monitor_idx}")
        if cap.devicename != "DP-1":
            failures.append(f"the fallback opened {cap.devicename!r}, "
                            "expected the first output")

        # 2. A connector name that is still there resolves to its index, and
        #    the restore token is carried into the portal call - that is what
        #    makes every launch after the first one silent.
        asked.clear()
        cap = ScreenCapture(devicename="HDMI-A-1", restore_token="remembered")
        if cap.monitor_idx != 1:
            failures.append(f"HDMI-A-1 resolved to {cap.monitor_idx}, want 1")
        if not asked or asked[-1].get("restore_token") != "remembered":
            failures.append("the restore token was not handed to the portal - "
                            "the user would be asked again on every launch")

        # 3. A name that is gone is not fatal, and the stale token is
        #    dropped: restoring a monitor that is not connected is exactly
        #    what the picker is for.
        asked.clear()
        cap = ScreenCapture(devicename="DP-9", restore_token="stale")
        if asked and asked[-1].get("restore_token"):
            failures.append("a stale token was still sent - the portal would "
                            "restore a monitor that is not there")

        # 4. Rotation: the capture produces the rotated picture, so the
        #    rotated numbers are the ones everything downstream is built
        #    from.
        outputs.append(fake_output("DP-2", 3840, 2160, transform=1))
        size = capture._physical_size(outputs[-1])
        if size != (2160, 3840):
            failures.append(f"a 90-degree output reports {size}, "
                            "expected the sides swapped")
        listed = dict((name, (w, h)) for _i, w, h, name
                      in capture.list_monitors())
        if listed.get("DP-2") != (2160, 3840):
            failures.append(f"list_monitors did not rotate DP-2: {listed}")
    finally:
        capture._outputs = real_outputs
        capture.refresh_outputs = real_refresh
        portal.open_screencast = real_open
        ScreenCapture._start_gstreamer = real_gst

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: stale indices fall back, names resolve, tokens are kept or "
          "dropped, rotation is applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
