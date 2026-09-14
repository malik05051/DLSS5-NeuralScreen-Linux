"""What a test needs before it can mean anything.

A third of this suite is not a unit test: it starts the real worker, takes
over the screen, or encodes video on the GPU. On the machine the program is
meant to run on, those are the most valuable tests here. Anywhere else - a
CI runner, a container, a laptop over SSH - they cannot run at all, and a
suite that reports them as failures teaches everyone to ignore red.

So they say what they need and skip when it is not there. A skip is printed,
counted and visible; it is not a pass pretending to be one.

    from _needs import needs_worker, needs_wayland

    def main() -> int:
        if skip := needs_worker():
            return skip
        ...

Each helper returns 0 when the requirement is missing - the exit code for a
skip, so the runner does not fail on it - and None when it is satisfied.
The message says which requirement, so a developer on a real machine can
tell "not run here" from "not run at all".
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paths import WORKER_EXE  # noqa: E402


def _skip(what: str, why: str) -> int:
    print(f"SKIP: {what} - {why}")
    return 0


def needs_worker():
    """The built worker: native/linux/neuralscreen-host, and a GPU under it."""
    if not WORKER_EXE.is_file():
        return _skip("needs the worker",
                     f"{WORKER_EXE} is not built (native/linux/build-host.sh)")
    # Built is not the same as usable: --probe opens a Vulkan device and
    # creates the feature, which is exactly what the test is about to need.
    try:
        probe = subprocess.run([str(WORKER_EXE), "--probe"],
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return _skip("needs the worker", f"--probe did not run ({exc})")
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip().splitlines()
        return _skip("needs an NVIDIA GPU with NGX",
                     detail[-1] if detail else "the worker's probe failed")
    return None


def needs_wayland():
    """A Wayland session to put a surface on."""
    if not os.environ.get("WAYLAND_DISPLAY") \
            and not os.environ.get("WAYLAND_SOCKET"):
        return _skip("needs a Wayland session", "WAYLAND_DISPLAY is not set")
    return None


def needs_layer_shell():
    """A compositor that implements wlr-layer-shell.

    Stronger than needs_wayland: GNOME is a Wayland session and has no layer
    shell, and the tests that check the overlay's stacking cannot mean
    anything there.
    """
    if skip := needs_wayland():
        return skip
    try:
        from wayland_shell import SHELL

        if SHELL.display is None:
            SHELL.connect()
        if SHELL.layer_shell is None:
            return _skip("needs wlr-layer-shell",
                         "this compositor does not offer it")
    except Exception as exc:
        return _skip("needs wlr-layer-shell", str(exc))
    return None


def needs_nvenc():
    """An NVENC encoder, for the recorder tests."""
    try:
        import av
    except ImportError as exc:
        return _skip("needs PyAV", str(exc))
    for name in ("av1_nvenc", "hevc_nvenc", "h264_nvenc"):
        try:
            # Opening the context, not just finding the codec: ffmpeg ships
            # the nvenc encoders compiled in on every distribution, so
            # merely resolving the name says nothing about whether there is
            # a card behind it. avcodec_open2 is the question that matters
            # and it is the same one recorder.py asks.
            codec = av.codec.CodecContext.create(name, "w")
            codec.width, codec.height = 320, 240
            codec.pix_fmt = "yuv420p"
            codec.open()
            codec.close()
            return None
        except Exception:
            continue
    return _skip("needs NVENC", "no nvenc encoder would open - no NVIDIA GPU")


def needs_portal(interface: str = "ScreenCast"):
    """A running xdg-desktop-portal with this interface."""
    try:
        import portal

        if not portal.available(interface):
            return _skip(f"needs the {interface} portal",
                         "no portal answered on the session bus")
    except Exception as exc:
        return _skip(f"needs the {interface} portal", str(exc))
    return None


def needs_tool(name: str):
    """An external command the test shells out to."""
    if shutil.which(name) is None:
        return _skip(f"needs {name}", "it is not on PATH")
    return None
