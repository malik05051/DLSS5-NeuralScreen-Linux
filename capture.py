"""ScreenCapture - desktop capture for the DLSS 5 NR prototype (desktop-nr).

Backend: xdg-desktop-portal ScreenCast, read as a PipeWire stream.
Frames come back as np.ndarray shape (H, W, 4) dtype uint8 in RGBA.

This is the module that changed most in the move off Windows, and the change
is not a swap of one API for another. Desktop Duplication was something a
program did to a monitor: open it, pull frames, no one asked. A Wayland
compositor does not let a client read another client's pixels at all, so the
capture is something the *user* grants, once, through a picker - and what
comes back is not a monitor but a PipeWire node that happens to carry one.

Three consequences the rest of the program has to know about:

  * **The first launch shows a dialog.** It cannot be avoided and should not
    be worked around. It can be avoided on every launch *after* the first:
    the portal hands back a `restore_token`, that token goes into the config,
    and a session opened with it restores the same screen silently. The
    Windows build stored a DXGI devicename for the same reason - to survive
    a reorder - and this is the same idea with the compositor's agreement.

  * **Monitor identity is the connector name** - "DP-1", "HDMI-A-2",
    "eDP-1" - not '\\\\.\\DISPLAY1'. It is better in the way that matters:
    it names the physical socket, so unplugging a second monitor and
    plugging it back in somewhere else keeps the name. `list_monitors()`
    returns it where the Windows version returned the DXGI name, and the
    saved config is compatible in shape though not in content, so a config
    carried over from Windows falls back to the picker once.

  * **There is no equivalent of "capture the whole virtual desktop".** The
    portal grants one source. That is not a limitation in practice - the
    program has always processed one monitor - but `monitor_origin()` now
    answers from the compositor's logical coordinate space rather than the
    virtual-desktop space Windows built out of monitor rectangles.

Frames on the Python side come through GStreamer's `pipewiresrc`, which is
the one PipeWire consumer that is both universally installed and has a
Python binding. It is only used when `capture_in_worker` is off: the normal
path is the worker importing the stream's dmabuf straight into Vulkan, which
never copies a pixel through Python at all - the same division of labour the
Windows build had between dxcam and the worker's own DDA.

Example:
    cap = ScreenCapture(monitor_idx=0)
    frame = cap.grab()          # (2160, 3840, 4) uint8 RGBA
    cap.close()
"""

from __future__ import annotations

import os
import sys
import threading
import time

import numpy as np

import portal
from wayland_shell import SHELL


#: The token that lets the next launch skip the picker. Owned by the config;
#: this module only reads and updates it, see `restore_token()`.
_RESTORE_TOKEN = ""

#: The outputs, cached. Enumerating means a Wayland roundtrip; the menu asks
#: on every frame it is open and two roundtrips per frame cost more than the
#: network does. Monitors are not hot-plugged often, and when they are the
#: compositor tells us and `refresh_outputs()` is called.
_OUTPUTS: list | None = None
_OUTPUTS_LOCK = threading.Lock()


def _ensure_shell() -> bool:
    """Make sure the Wayland connection is up. False on a machine with none."""
    if SHELL.display is not None:
        return True
    return SHELL.connect()


def refresh_outputs() -> None:
    """Forget the cached output list.

    The counterpart of the Windows build's `_refresh_dxcam_factory`: called
    when a monitor was unplugged or the arrangement changed and the cached
    indices are stale.
    """
    global _OUTPUTS
    with _OUTPUTS_LOCK:
        _OUTPUTS = None
    if SHELL.display is not None:
        try:
            SHELL.display.roundtrip()
        except Exception:
            pass


#: Kept under its Windows name so the call sites that mean "re-enumerate"
#: still say so. There is no dxcam and no factory; there is a compositor
#: that was asked again.
_refresh_dxcam_factory = refresh_outputs


def _outputs() -> list:
    global _OUTPUTS
    with _OUTPUTS_LOCK:
        if _OUTPUTS is not None:
            return _OUTPUTS
    if not _ensure_shell():
        return []
    found = SHELL.output_list()
    with _OUTPUTS_LOCK:
        _OUTPUTS = found
    return found


def _output_count() -> int:
    return len(_outputs())


def resolve_output_idx(devicename: str) -> int | None:
    """The index of the output with this connector name, or None when gone."""
    for idx, out in enumerate(_outputs()):
        if out.name == devicename:
            return idx
    return None


def devicename_for_output_idx(output_idx: int) -> str | None:
    """The connector name of the output at this index, or None.

    The inverse of resolve_output_idx - used when saving the config so the
    monitor is remembered by identity instead of by a positional index.
    """
    outputs = _outputs()
    if 0 <= output_idx < len(outputs):
        return outputs[output_idx].name or None
    return None


#: The adapter list, enumerated once (see list_adapters).
_ADAPTERS: list | None = None


def list_adapters() -> list[tuple[int, str]]:
    """NVIDIA cards as [(index, name), ...], in NVML enumeration order.

    The index is what matters: it is what the worker's NS_GPU takes and what
    its "[host] adapter N: ..." lines print, so the menu and the log agree on
    which card is which. On Linux that index is also what
    CUDA_VISIBLE_DEVICES and Vulkan's physical-device order are keyed to,
    which is one fewer translation than the DXGI adapter index needed.

    Only NVIDIA: the network cannot run anywhere else. A machine with one
    card gets a one-item list, and the menu hides the choice.

    Enumerated once per process and kept: menu_payload runs on every frame
    while the menu is open. Cards are not hot-plugged, and moving the worker
    to another one restarts it anyway.
    """
    global _ADAPTERS
    if _ADAPTERS is not None:
        return _ADAPTERS
    import gpuinfo

    out = gpuinfo.list_gpus()
    _ADAPTERS = out
    return out


def monitor_origin(devicename: str) -> tuple[int, int] | None:
    """The chosen monitor's top-left corner in the compositor's coordinates.

    Wayland places every output in one logical space, exactly as Windows put
    every monitor on one virtual desktop, and for the same reason the
    overlay has to know it: the layer surface and the frame both have to land
    on the screen the capture is running on (issues #28, #33).

    Answered from xdg_output's logical position, which is the space the
    pointer and the portal's own stream metadata use. wl_output's raw
    position is in physical pixels and disagrees the moment any monitor is
    scaled.
    """
    for out in _outputs():
        if out.name == devicename:
            return (out.x, out.y)
    return None


def monitor_size(devicename: str) -> tuple[int, int] | None:
    """The CURRENT size of one monitor, by connector name, or None.

    Asked live: st.capture.resolution is what the monitor was when the
    capture session opened, and the resolution can change under a running
    pipeline (the user switching 1440p -> 4K, a game changing the mode, a
    dock). A Wayland roundtrip is the cheap check the loop can afford
    between frames.
    """
    if not _ensure_shell():
        return None
    try:
        SHELL.display.roundtrip()
    except Exception:
        return None
    for out in SHELL.output_list():
        if out.name == devicename:
            return _physical_size(out)
    return None


def _physical_size(out) -> tuple[int, int]:
    """The pixel size of an output, with rotation taken into account.

    wl_output reports the mode, which is the panel's own orientation: a
    monitor rotated 90 degrees still says 3840x2160 while the desktop on it
    is 2160x3840. The capture produces the rotated picture, so the rotated
    numbers are the ones every size downstream has to be built from. The
    Windows build could not do this at all - 90 and 270 came out with the
    sides swapped, and it is listed as a known limitation there.
    """
    w, h = out.width, out.height
    if not w or not h:
        w, h = out.logical_w * out.scale, out.logical_h * out.scale
    if out.rotated:
        w, h = h, w
    return (int(w), int(h))


def list_monitors() -> list[tuple[int, int, int, str]]:
    """Monitors as [(idx, w, h, devicename), ...].

    idx is the position in the output list sorted by corner, which is stable
    across a session and is what the menu shows. devicename is the connector
    name, which is stable across everything and is what the config saves.
    Sizes are physical pixels, rotation applied.
    """
    out: list[tuple[int, int, int, str]] = []
    for idx, output in enumerate(_outputs()):
        w, h = _physical_size(output)
        out.append((idx, w, h, output.name))
    return out


def monitor_label(devicename: str) -> str:
    """A human name for a monitor: "DP-1 - Dell U2720Q", or just the name.

    The menu had nothing to show but '\\\\.\\DISPLAY2' on Windows. The
    compositor knows the make and model, so the menu can say which monitor
    is which without the user counting sockets.
    """
    for out in _outputs():
        if out.name == devicename:
            return f"{out.name} - {out.description}" if out.description \
                else out.name
    return devicename


# ---------------------------------------------------------------------------
# The capture itself
# ---------------------------------------------------------------------------


class ScreenCapture:
    """Monitor capture through the portal, read with GStreamer's pipewiresrc.

    The Windows build kept a GDI fallback here for hybrid laptops where
    Desktop Duplication refused a cross-adapter session. That whole class of
    failure is gone: the compositor composites the frame whatever GPU it
    happens to live on, and a PipeWire buffer that cannot be imported
    directly is delivered as memory instead. What replaces the fallback is
    a format negotiation, and the one thing it can still fail at - no
    GStreamer - is reported plainly rather than limped past, because the
    worker's own capture path is the normal one and this is the fallback.
    """

    def __init__(self, monitor_idx: int = 0, devicename: str | None = None,
                 restore_token: str = ""):
        self.monitor_idx = monitor_idx
        self.devicename = devicename or ""
        self.resolution = (0, 0)
        self.restore_token = restore_token or _RESTORE_TOKEN
        self._session = None
        self._pipeline = None
        self._sink = None
        self._frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()
        self._error = ""

        if devicename is not None:
            resolved = resolve_output_idx(devicename)
            if resolved is None:
                # Not fatal, unlike the Windows build's ValueError: the
                # portal is about to ask the user anyway, and a monitor that
                # was unplugged since the config was written is exactly the
                # case the picker exists for.
                print(f"[capture] no output is called {devicename!r} any more "
                      "- the portal will ask which screen to use",
                      file=sys.stderr)
                self.restore_token = ""
            else:
                monitor_idx = resolved
        outputs = _outputs()
        if outputs and monitor_idx >= len(outputs):
            print(f"[capture] output {monitor_idx} is gone - re-enumerating",
                  file=sys.stderr)
            refresh_outputs()
            outputs = _outputs()
            if monitor_idx >= len(outputs):
                print("[capture] falling back to output 0", file=sys.stderr)
                monitor_idx = 0
        self.monitor_idx = monitor_idx
        if outputs:
            self.devicename = outputs[min(monitor_idx, len(outputs) - 1)].name
            self.resolution = _physical_size(outputs[min(monitor_idx,
                                                         len(outputs) - 1)])
        self._open()

    # -- negotiation -------------------------------------------------------

    def _open(self, source_types: int = portal.SOURCE_MONITOR) -> None:
        global _RESTORE_TOKEN
        self._session = portal.open_screencast(source_types=source_types,
                                               restore_token=self.restore_token)
        if not self._session.ok:
            self._error = "the portal did not grant a capture"
            return
        if self._session.restore_token:
            self.restore_token = self._session.restore_token
            _RESTORE_TOKEN = self.restore_token
        if self._session.size != (0, 0):
            self.resolution = self._session.size
        self._start_gstreamer()

    @property
    def node_id(self) -> int:
        """The PipeWire node the worker should open. -1 when there is none."""
        return self._session.node_id if self._session is not None else -1

    @property
    def pipewire_fd(self) -> int:
        """The PipeWire remote fd, for handing to the worker. -1 when none."""
        return self._session.fd if self._session is not None else -1

    @property
    def error(self) -> str:
        return self._error

    # -- the GStreamer side ------------------------------------------------

    def _start_gstreamer(self) -> None:
        """Build pipewiresrc ! videoconvert ! appsink, and run it.

        `videoconvert` rather than demanding RGBA from the source: what the
        compositor offers depends on the compositor and the GPU (BGRx is the
        common one, and on some setups only a dmabuf format is offered at
        all). Converting costs a copy on the fallback path, and the fallback
        path is the one that was already copying through Python.
        """
        try:
            import gi
            gi.require_version("Gst", "1.0")
            gi.require_version("GstApp", "1.0")
            from gi.repository import Gst, GstApp     # noqa: F401
        except Exception as exc:
            self._error = (
                "GStreamer with the PipeWire plugin is needed to read frames "
                f"in Python ({exc}). The worker's own capture does not need "
                "it - leave \"capture_in_worker\" on.")
            print(f"[capture] {self._error}", file=sys.stderr)
            return
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)
        # The fd belongs to the session and is closed with it; GStreamer
        # duplicates it rather than taking ownership.
        description = (
            f"pipewiresrc fd={self._session.fd} path={self._session.node_id} "
            "always-copy=true ! videoconvert ! "
            "video/x-raw,format=RGBA ! "
            "appsink name=ns emit-signals=true max-buffers=1 drop=true sync=false")
        try:
            self._pipeline = Gst.parse_launch(description)
            self._sink = self._pipeline.get_by_name("ns")
            self._sink.connect("new-sample", self._on_sample)
            self._pipeline.set_state(Gst.State.PLAYING)
        except Exception as exc:
            self._error = f"the GStreamer pipeline would not start: {exc}"
            print(f"[capture] {self._error}", file=sys.stderr)
            self._pipeline = None

    def _on_sample(self, sink):
        from gi.repository import Gst

        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        width = caps.get_value("width")
        height = caps.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            # A copy, and it has to be: the mapping is unmapped the moment
            # this returns and grab() hands the array to a pipeline that
            # holds it for a frame or two.
            frame = np.frombuffer(info.data, dtype=np.uint8,
                                  count=width * height * 4).reshape(
                                      height, width, 4).copy()
        finally:
            buf.unmap(info)
        with self._frame_lock:
            self._frame = frame
            self.resolution = (width, height)
        return Gst.FlowReturn.OK

    # -- the same surface the Windows version had --------------------------

    @classmethod
    def resolve_monitor(cls, devicename: str) -> int | None:
        """The output index for a connector name, or None when it is gone."""
        return resolve_output_idx(devicename)

    def grab(self) -> np.ndarray | None:
        """Grab the monitor's current frame.

        Returns:
            np.ndarray shape (H, W, 4) dtype uint8, RGBA channels,
            C-contiguous (frombuffer/tobytes without a copy).
            None when no frame has arrived yet - which, exactly like
            DXGI_ERROR_WAIT_TIMEOUT on the duplication path, means the screen
            has not changed, not that anything is wrong.

        Every grab() hands back a separate array: the appsink copies each
        buffer out of its mapping, so a frame can be held across a loop
        iteration.
        """
        with self._frame_lock:
            frame, self._frame = self._frame, None
        return frame

    def close(self) -> None:
        """Release the capture resources.

        Order matters: the GStreamer pipeline holds a duplicate of the
        PipeWire fd and must be stopped before the portal session that owns
        the original is closed, or the compositor keeps composing frames for
        a stream nobody is reading.
        """
        if self._pipeline is not None:
            try:
                from gi.repository import Gst
                self._pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            self._pipeline = None
            self._sink = None
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> "ScreenCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def restore_token() -> str:
    """The token the config should remember so the next launch is silent."""
    return _RESTORE_TOKEN


def set_restore_token(token: str) -> None:
    global _RESTORE_TOKEN
    _RESTORE_TOKEN = token or ""


def capture_window(restore_token: str = "") -> "portal.ScreenCastSession":
    """Ask the user to pick a window, and return the granted session.

    This is what Num5 became. On Windows the program found the window under
    the cursor itself and started a Windows Graphics Capture on it; here a
    client cannot enumerate other clients' windows, let alone point at one,
    so the compositor's own picker does the choosing. It is one extra click
    the first time and none afterwards - the restore token covers a window
    the same way it covers a monitor.
    """
    return portal.open_screencast(source_types=portal.SOURCE_WINDOW,
                                  restore_token=restore_token)
