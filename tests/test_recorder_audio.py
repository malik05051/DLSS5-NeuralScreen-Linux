"""The recording carries a second track with the system audio.

Plays a tone into the default playback device while recording, then reads the
file back and checks that:
  * the file has two tracks and the audio one is AAC;
  * the audio is real sound, not a track of zeros;
  * audio and video cover the same stretch of time - the whole point of the
    padding in VideoRecorder is that they must not drift apart;
  * video is unaffected: the frames are still all there.

A machine with no playback endpoint is not a failure - the test says SKIP.
"""
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import av
import numpy as np

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)
from recorder import VideoRecorder  # noqa: E402

from _needs import needs_nvenc, needs_tool  # noqa: E402

W, H = 1280, 720
FPS = 30.0
FRAMES = 90            # 3 seconds at 30 fps
TONE_RATE = 48000


def tone_wav(path: Path, seconds: float) -> None:
    n = int(TONE_RATE * seconds)
    t = np.arange(n, dtype=np.float32) / TONE_RATE
    pcm = (np.sin(2 * np.pi * 440.0 * t) * 0.5 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(TONE_RATE)
        w.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())


def make_frame(i: int) -> np.ndarray:
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[..., 0] = (i * 3) % 256
    frame[..., 1] = 60
    frame[..., 2] = 120
    frame[..., 3] = 255
    return np.ascontiguousarray(frame)


def main() -> int:
    if (skip := needs_nvenc()) is not None:
        return skip
    if (skip := needs_tool("paplay")) is not None:
        return skip
    failures = []
    out = Path(tempfile.gettempdir()) / "ns-test-audio.mp4"
    tone = Path(tempfile.gettempdir()) / "ns-test-tone.wav"
    out.unlink(missing_ok=True)
    tone_wav(tone, FRAMES / FPS + 1.0)

    rec = VideoRecorder(str(out), W, H, fps=FPS, audio=True)
    if rec._audio is None:
        rec.close()
        out.unlink(missing_ok=True)
        tone.unlink(missing_ok=True)
        print("SKIP: no playback endpoint, there is nothing to record")
        return 0
    rate = rec._audio.sample_rate
    print(f"endpoint {rate} Hz")

    # Two phases on purpose. While the tone plays the loopback delivers real
    # packets; once it stops, the idle endpoint delivers nothing at all, and
    # only the padding keeps the track growing. The second phase is what
    # actually exercises AUDIO_GAP_S/AUDIO_LAG_S.
    # The Windows version used winsound to play a tone into the loopback.
    # paplay is the PulseAudio/PipeWire equivalent and is part of the same
    # package as pactl, which the capture already needs.
    player = subprocess.Popen(["paplay", str(tone)],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    t0 = time.perf_counter()
    try:
        for i in range(FRAMES):
            if i == FRAMES // 2:
                player.terminate()              # silence from here on
            rec.write(make_frame(i))
            time.sleep(1.0 / FPS)
    finally:
        wall = time.perf_counter() - t0
        rec.close()
        player.terminate()
        tone.unlink(missing_ok=True)

    print(f"recorded {wall:.2f} s of wall time, "
          f"{rec.written} frames written, {rec.dropped} dropped, "
          f"{rec.audio_padded / rate:.2f} s padded")

    if not out.is_file() or out.stat().st_size == 0:
        print("FAIL: the file was not created")
        return 1

    with av.open(str(out)) as c:
        vstreams = c.streams.video
        astreams = c.streams.audio
        print(f"tracks: {len(vstreams)} video, {len(astreams)} audio")
        if not astreams:
            print("FAIL: no audio track in the file")
            return 1
        acodec = astreams[0].codec_context.name
        vcount = 0
        samples = 0
        peak = 0.0
        for frame in c.decode(video=0):
            vcount += 1
        c.seek(0)
        for frame in c.decode(audio=0):
            arr = frame.to_ndarray()
            samples += arr.shape[-1]
            peak = max(peak, float(np.abs(arr).max()))

    a_secs = samples / rate
    v_secs = vcount / FPS
    print(f"audio: {acodec}, {samples} samples = {a_secs:.2f} s, peak {peak:.3f}")
    print(f"video: {vcount} frames = {v_secs:.2f} s")

    if acodec != "aac":
        failures.append(f"the audio codec is {acodec}, not aac")
    if peak < 0.01:
        failures.append(f"the audio track is silence (peak {peak:.4f})")
    if a_secs < wall * 0.8:
        failures.append(f"the audio is short: {a_secs:.2f} s against {wall:.2f} s of wall time")
    if abs(a_secs - wall) > 0.5:
        failures.append(f"audio and the wall clock drifted apart: "
                        f"{a_secs:.2f} s vs {wall:.2f} s")
    # The second half was recorded in silence, so either the endpoint kept
    # feeding silent packets or the padding did its job - but the track must
    # not be short. A track that is only as long as the tone means the gap was
    # simply dropped, and everything after it would be out of sync.
    if a_secs < wall * 0.9:
        failures.append(f"the silent stretch was lost: {a_secs:.2f} s of {wall:.2f} s")
    if vcount < rec.written:
        failures.append(f"the file holds {vcount} frames, {rec.written} were written")

    out.unlink(missing_ok=True)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: two tracks, real sound, audio and video in step")
    return 0


if __name__ == "__main__":
    sys.exit(main())
