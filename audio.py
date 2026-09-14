"""LoopbackCapture - system audio ("what you hear") from the default sink's
monitor.

Records what the default playback device is playing, so a recording carries
the game or video sound without a virtual cable and without a microphone.

The Windows build did this with WASAPI loopback, which was the only way and
came with a trap: while nothing plays at all, the endpoint hands back *no*
data rather than silence, so a recorder that simply concatenates what it
gets ends up with audio shorter than the video and drifting away from it.
PipeWire and PulseAudio have no such trap - a monitor source produces
silence when the sink is idle, at the rate it promised - but the padding in
recorder.py is kept anyway: it costs nothing when there is nothing to pad,
and the one thing worse than an audio track that drifts is an audio track
that drifts only on some machines.

Every Linux desktop that plays sound at all has a monitor source. On
PipeWire the PulseAudio API is served by pipewire-pulse and the monitor is
the same object; on a PulseAudio system it is native. So this talks
libpulse's synchronous API - twenty lines of ctypes, no dependency, and
correct on both - rather than choosing between two client libraries.

Usage:
    cap = LoopbackCapture()
    cap.start()
    while ...:
        chunk = cap.read()      # (n, 2) float32 or None
    cap.close()
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import subprocess
import sys
import threading
import time

import numpy as np


# pa_sample_format: 3 is PA_SAMPLE_FLOAT32LE, which is what the server mixes
# in anyway, so asking for it costs no conversion in the daemon and saves
# one here.
PA_SAMPLE_FLOAT32LE = 3
PA_STREAM_RECORD = 2

#: The special source name PulseAudio resolves to "the monitor of whatever
#: the default sink currently is". Following the default is the whole point:
#: a user plugging in headphones mid-recording should keep being recorded.
DEFAULT_MONITOR = "@DEFAULT_MONITOR@"


class pa_sample_spec(ctypes.Structure):
    _fields_ = [("format", ctypes.c_int),
                ("rate", ctypes.c_uint32),
                ("channels", ctypes.c_uint8)]


class pa_buffer_attr(ctypes.Structure):
    _fields_ = [("maxlength", ctypes.c_uint32), ("tlength", ctypes.c_uint32),
                ("prebuf", ctypes.c_uint32), ("minreq", ctypes.c_uint32),
                ("fragsize", ctypes.c_uint32)]


def _load_pulse():
    """libpulse-simple, or None. Never raises."""
    for name in ("libpulse-simple.so.0", "libpulse-simple.so",
                 ctypes.util.find_library("pulse-simple")):
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        lib.pa_simple_new.restype = ctypes.c_void_p
        lib.pa_simple_new.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.POINTER(pa_sample_spec), ctypes.c_void_p,
            ctypes.POINTER(pa_buffer_attr), ctypes.POINTER(ctypes.c_int)]
        lib.pa_simple_read.restype = ctypes.c_int
        lib.pa_simple_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_int)]
        lib.pa_simple_free.argtypes = [ctypes.c_void_p]
        lib.pa_strerror.restype = ctypes.c_char_p
        lib.pa_strerror.argtypes = [ctypes.c_int]
        return lib
    return None


def default_monitor() -> str:
    """The monitor source to record, resolved as concretely as possible.

    `@DEFAULT_MONITOR@` is the right answer and the server usually honours
    it. When it does not - an old PulseAudio, a pipewire-pulse built
    without the alias - the name is looked up with pactl, which every
    desktop that has sound has. Falling back to the literal alias is
    harmless: pa_simple_new simply fails and start() reports why.
    """
    pactl = shutil.which("pactl")
    if pactl:
        try:
            sink = subprocess.run([pactl, "get-default-sink"],
                                  capture_output=True, text=True, timeout=3)
            name = sink.stdout.strip()
            if name and not name.startswith("Failure"):
                return f"{name}.monitor"
        except (OSError, subprocess.SubprocessError):
            pass
    return DEFAULT_MONITOR


class LoopbackCapture:
    """The default sink's monitor, read on its own thread.

    All the library work lives in one thread, as it did for COM on Windows -
    not because libpulse needs it, but because the reader blocks and the
    caller is a 60 fps loop that must not.

    Output is always float32 (n, 2): the encoder wants one shape and does not
    care what the device happens to run at. The sample rate is whatever the
    monitor reports (sample_rate) - resampling here would be pointless work,
    the AAC encoder takes 48 kHz just as happily as 44.1.
    """

    #: How much audio to ask for per read. 20 ms: long enough that the
    #: syscall rate is irrelevant, short enough that stopping a recording
    #: does not wait a visible time for the last block.
    CHUNK_MS = 20

    #: Above this the soft limiter starts folding. The system mix can hand
    #: back peaks above 0 dBFS (measured up to +7.9 dB on the bench) and AAC
    #: turns those into distortion.
    LIMIT_THRESHOLD = 0.85

    def __init__(self, device: str = ""):
        self.sample_rate = 0
        self.channels = 0
        self.device = device
        self.error: str | None = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    # -- public API --------------------------------------------------------

    def start(self, timeout: float = 5.0) -> bool:
        """Start capturing. Returns False when audio is unavailable.

        Never raises: a machine with no sound server, or a container with no
        access to one, must still record video.
        """
        self._thread = threading.Thread(target=self._run, name="ns-audio",
                                        daemon=True)
        self._thread.start()
        self._started.wait(timeout)
        return self.error is None and self.sample_rate > 0

    def read(self) -> np.ndarray | None:
        """Everything captured since the previous call, or None if nothing.

        Returns float32 (n, 2).
        """
        with self._lock:
            if not self._chunks:
                return None
            chunks, self._chunks = self._chunks, []
        return chunks[0] if len(chunks) == 1 else np.concatenate(chunks, axis=0)

    def discard(self) -> None:
        """Drop everything captured so far (the stream spin-up).

        The samples that arrive between the stream opening and the
        recorder's clock are earlier than the video PTS=0; keeping them
        would make the audio track lead the picture (audit #3, D2).
        """
        with self._lock:
            self._chunks.clear()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # -- capture thread ----------------------------------------------------

    def _run(self) -> None:
        stream = None
        lib = _load_pulse()
        if lib is None:
            self.error = ("libpulse-simple is not installed - no sound server "
                          "client library to record the system mix with")
            self._started.set()
            return
        try:
            device = self.device or default_monitor()
            # 48 kHz stereo: the rate every desktop mixes at, and the one the
            # AAC encoder wants. Asking for the device's own rate would mean
            # the async API; asking for this one lets the server resample,
            # which it does better than we would.
            spec = pa_sample_spec(PA_SAMPLE_FLOAT32LE, 48000, 2)
            frame_bytes = 4 * spec.channels
            fragment = int(spec.rate * self.CHUNK_MS / 1000) * frame_bytes
            attr = pa_buffer_attr(maxlength=0xFFFFFFFF, tlength=0xFFFFFFFF,
                                  prebuf=0xFFFFFFFF, minreq=0xFFFFFFFF,
                                  fragsize=fragment)
            err = ctypes.c_int(0)
            stream = lib.pa_simple_new(
                None, b"NeuralScreen", PA_STREAM_RECORD,
                device.encode("utf-8"), b"screen recording",
                ctypes.byref(spec), None, ctypes.byref(attr),
                ctypes.byref(err))
            if not stream:
                detail = lib.pa_strerror(err).decode("utf-8", "replace")
                self.error = f"could not open {device}: {detail}"
                self._started.set()
                return
            self.sample_rate = spec.rate
            self.channels = spec.channels
            self._started.set()
            self._pump(lib, stream, fragment, frame_bytes)
        except Exception as exc:
            self.error = str(exc)
            self._started.set()
        finally:
            if stream:
                try:
                    lib.pa_simple_free(ctypes.c_void_p(stream))
                except Exception:
                    pass

    def _pump(self, lib, stream, fragment: int, frame_bytes: int) -> None:
        buf = ctypes.create_string_buffer(fragment)
        err = ctypes.c_int(0)
        while not self._stop.is_set():
            if lib.pa_simple_read(ctypes.c_void_p(stream), buf, fragment,
                                  ctypes.byref(err)) < 0:
                detail = lib.pa_strerror(err).decode("utf-8", "replace")
                # A monitor that goes away (the sink was removed) is not a
                # reason to stop the recording - the video keeps going and
                # the audio track simply ends where the device did.
                self.error = f"the monitor source stopped: {detail}"
                return
            samples = np.frombuffer(buf.raw, dtype=np.float32).reshape(
                -1, self.channels).copy()
            with self._lock:
                self._chunks.append(self._to_stereo(samples))

    # -- shaping -----------------------------------------------------------

    @staticmethod
    def _to_stereo(arr: np.ndarray, scale: float = 1.0) -> np.ndarray:
        """Fold any channel count down to stereo, scaled.

        A 5.1 monitor is a real thing on a desktop with a receiver. Front
        left/right carry the mix; the centre is folded in at -3 dB, which is
        the downmix every player uses, and the rest are dropped rather than
        guessed at.
        """
        arr = np.asarray(arr, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        channels = arr.shape[1]
        if channels == 1:
            out = np.repeat(arr, 2, axis=1)
        elif channels == 2:
            out = arr
        else:
            out = arr[:, :2].copy()
            if channels >= 3:
                out += arr[:, 2:3] * 0.7071
        if scale != 1.0:
            out = out * scale
        # The limiter belongs here, not only in the caller: a downmix adds
        # channels together and can push a mix that was inside [-1, 1] over
        # the top, and this is the one funnel every chunk goes through.
        return LoopbackCapture._limit(out)

    @staticmethod
    def _limit(x: np.ndarray) -> np.ndarray:
        """Fold peaks above the threshold toward 1.0 with a tanh tail.

        Below the threshold nothing is touched - bit for bit, which is what
        the regression test checks, and what makes this safe to run on every
        chunk instead of only on loud ones. Above it the excess is folded
        so the result never leaves [-1, 1] and never changes sign.
        """
        threshold = LoopbackCapture.LIMIT_THRESHOLD
        peak = np.max(np.abs(x)) if x.size else 0.0
        if peak <= threshold:
            return x
        out = x.copy()
        over = np.abs(out) > threshold
        excess = np.abs(out[over]) - threshold
        folded = threshold + (1.0 - threshold) * np.tanh(
            excess / (1.0 - threshold))
        out[over] = np.sign(out[over]) * folded
        return out
