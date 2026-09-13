"""Threaded encoding: write() does not block and the file comes out whole.

Checks the three things the change was made for:
  * write() returns quickly - otherwise the thread is pointless;
  * frames are not lost at a normal pace and do end up in the file;
  * close() waits for the encoder and the file reads back.

Plus an honesty check: frames are handed to the thread WITHOUT a copy, so we
verify the written picture matches the one handed over, not the last in queue.
"""
import sys
import tempfile
import time
from pathlib import Path

import av
import numpy as np

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)
from recorder import VideoRecorder  # noqa: E402

from _needs import needs_nvenc  # noqa: E402

W, H = 1920, 1080
FRAMES = 40
FPS = 60.0


def make_frame(i: int) -> np.ndarray:
    """Every frame gets its own tint - so they can be told apart in the file."""
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[..., 0] = (i * 6) % 256      # R grows with the index
    frame[..., 1] = 40
    frame[..., 2] = 90
    frame[..., 3] = 255
    return np.ascontiguousarray(frame)


def main() -> int:
    if (skip := needs_nvenc()) is not None:
        return skip
    failures = []
    out = Path(tempfile.gettempdir()) / "ns-test-recorder.mp4"
    out.unlink(missing_ok=True)

    # audio=False on purpose: this test is about the video encode thread, and
    # a loopback that is missing or busy would muddy its timings.
    # The audio track has its own test - test_recorder_audio.py.
    rec = VideoRecorder(str(out), W, H, fps=FPS, audio=False)
    worst = 0.0
    try:
        for i in range(FRAMES):
            frame = make_frame(i)
            t0 = time.perf_counter()
            rec.write(frame)
            worst = max(worst, (time.perf_counter() - t0) * 1000.0)
            time.sleep(1.0 / FPS)      # the pipeline pace
    finally:
        rec.close()

    print(f"worst write(): {worst:.1f} ms, dropped {rec.dropped}, "
          f"written {rec.written}")
    # The synchronous path cost ~20 ms at 4K; at 1080p about 5. The threshold
    # has slack: if the change works, write() is only a put into the queue.
    if worst > 3.0:
        failures.append(f"write() blocked for {worst:.1f} ms - the thread did not help")
    if rec.dropped > FRAMES * 0.1:
        failures.append(f"dropped {rec.dropped} of {FRAMES} - the queue is too small")
    if rec.written + rec.dropped != FRAMES:
        failures.append(f"frames accounted for {rec.written}+{rec.dropped}, "
                        f"but {FRAMES} were handed over")

    if not out.is_file() or out.stat().st_size == 0:
        print("FAIL: the file was not created")
        return 1
    with av.open(str(out)) as container:
        stream = container.streams.video[0]
        decoded = [f for f in container.decode(stream)]
    print(f"file {out.stat().st_size / 1024:.0f} KB, decoded "
          f"{len(decoded)} frames, {stream.codec_context.name}")
    if len(decoded) < rec.written:
        failures.append(f"the file holds {len(decoded)} frames, but "
                        f"{rec.written} were written")
    if decoded:
        # The first frame must be the first one handed over, not one from the queue
        rgb = decoded[0].to_ndarray(format="rgb24")
        r = int(rgb[..., 0].mean())
        print(f"first frame: R={r} (about 0 expected)")
        if r > 40:
            failures.append(f"the first frame is not the first handed over (R={r})")

    out.unlink(missing_ok=True)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: write() does not block, the frames are there, the file reads back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
