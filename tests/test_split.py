"""Check the before/after wipe: the left part of the frame stays unprocessed.

The worker is driven directly, without a window and without Desktop
Duplication: we send the colour ourselves, so the "raw capture" is exactly our
input frame and it can be compared with the result pixel by pixel.

Expectation: to the left of the wipe the output matches the input bit for bit,
to the right it differs (NGX ran there), and on the boundary itself there is a
divider strip in the accent colour.
"""
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

from _needs import needs_worker  # noqa: E402

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

from main import (FRAME_FLAG_SPLIT, FRAME_FLAG_WANT_PIXELS, FRAME_FMT,  # noqa: E402
                  FRAME_MAGIC, HEADER_FMT, OUT_FMT, OUT_MAGIC, PROFILES,
                  VIDEO_MAGIC, WORKER_EXE)

W, H = 1280, 720          # full-res frame
WORK_W, WORK_H = 1280, 720  # 1:1, no upscale - easier to compare
SPLIT = 0.5
WARMUP = 8


def make_frame(seed: int) -> np.ndarray:
    """A frame with detail: on flat colour NGX has nothing to change and the
    test would go blind."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    base = ((xx * 7 + yy * 13) % 256).astype(np.uint8)
    noise = rng.integers(0, 48, size=(H, W), dtype=np.uint8)
    frame[..., 0] = base
    frame[..., 1] = (base // 2 + noise).astype(np.uint8)
    frame[..., 2] = (255 - base).astype(np.uint8)
    frame[..., 3] = 255
    return np.ascontiguousarray(frame)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"the worker closed stdout (got {len(buf)} of {n})")
        buf += chunk
    return buf


def send_and_get(worker, index: int, frame: np.ndarray, motion: np.ndarray,
                 split: float) -> np.ndarray | None:
    flags = FRAME_FLAG_WANT_PIXELS
    if split > 0.0:
        frac = min(0xFFFF, max(0, int(round(split * 0xFFFF))))
        flags |= FRAME_FLAG_SPLIT | (frac << 16)
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                   1 if index == 0 else 0, flags, index))
    worker.stdin.write(frame.tobytes())
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()

    head = read_exact(worker.stdout, struct.calcsize(OUT_FMT))
    magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
    if magic != OUT_MAGIC:
        raise RuntimeError(f"foreign reply 0x{magic:08X}")
    if not ok:
        raise RuntimeError(f"the worker returned ok=0, ngx=0x{ngx:08X}")
    if nbytes == 0:
        return None
    return np.frombuffer(read_exact(worker.stdout, nbytes),
                         dtype=np.uint8).reshape(H, W, 4).copy()


def main() -> int:
    if (skip := needs_worker()) is not None:
        return skip
    if not WORKER_EXE.is_file():
        print(f"FAIL: worker not found: {WORKER_EXE}")
        return 1
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(
        HEADER_FMT, VIDEO_MAGIC, WORK_W, WORK_H, WARMUP, 0,
        0, 0, int(params.get("style", 0)), int(params.get("auto_mask", 0)),
        int(params.get("ui_correction", 0)),
        float(params["intensity"]), float(params["local_tone"]),
        float(params["local_structure"]), float(params["skin_structure"]),
        0, 0)

    worker = subprocess.Popen([str(WORKER_EXE), "--live"],
                              cwd=str(WORKER_EXE.parent),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    failures = []
    try:
        worker.stdin.write(header)
        worker.stdin.flush()
        motion = np.zeros((WORK_H, WORK_W, 2), dtype=np.float16)

        # A couple of frames without the wipe - the pipeline must come alive
        # and change something
        frame = make_frame(1)
        plain = None
        for i in range(2):
            plain = send_and_get(worker, i, frame, motion, 0.0)
        if plain is None:
            print("FAIL: the worker returned no pixels")
            return 1
        changed = float((plain[..., :3] != frame[..., :3]).any(axis=2).mean())
        print(f"without the wipe NGX changed {changed:.1%} of the pixels")
        if changed < 0.10:
            failures.append(f"NGX changed almost nothing ({changed:.1%}) - "
                            f"the test could not tell the halves apart")

        out = send_and_get(worker, 2, frame, motion, SPLIT)
        if out is None:
            print("FAIL: no pixels came back with the wipe on")
            return 1
        split_x = int(W * SPLIT)
        # The divider of width lw straddles the boundary, so the halves are
        # compared away from it (see SplitCompose).
        lw = 3 if H >= 1400 else 2
        d0, d1 = split_x - lw // 2, split_x - lw // 2 + lw
        left_same = bool(np.array_equal(out[:, :d0, :3], frame[:, :d0, :3]))
        right_diff = float((out[:, d1:, :3] != frame[:, d1:, :3]).any(axis=2).mean())
        print(f"wipe at x={split_x}: the left side matches the input - "
              f"{'yes' if left_same else 'NO'}; the right side changed {right_diff:.1%}")
        if not left_same:
            bad = int((out[:, :d0, :3] != frame[:, :d0, :3]).any(axis=2).sum())
            failures.append(f"the left half does not equal the input ({bad} pixels)")
        if right_diff < 0.10:
            failures.append(f"the right half barely differs ({right_diff:.1%})")

        # The divider: every pixel of the strip must be exactly the accent colour
        accent = np.array([0xD9, 0x77, 0x57], dtype=np.uint8)
        band = out[:, d0:d1, :3]
        on_accent = float((band == accent).all(axis=2).mean())
        print(f"divider x={d0}..{d1 - 1}: accent colour over {on_accent:.1%} of the strip")
        if on_accent < 0.99:
            uniq = np.unique(band.reshape(-1, 3), axis=0)[:4]
            failures.append(f"the divider strip is the wrong colour "
                            f"({on_accent:.1%}), first colours: {uniq.tolist()}")

        # And with the wipe off there must be no strip at all
        plain_band = plain[:, d0:d1, :3]
        if float((plain_band == accent).all(axis=2).mean()) > 0.5:
            failures.append("the divider is drawn even with the wipe switched off")
    finally:
        try:
            worker.stdin.close()
        except OSError:
            pass
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
        err = worker.stderr.read().decode("utf-8", "replace")
        tail = [l for l in err.splitlines() if "pure" in l or "error" in l.lower()]
        if tail:
            print("worker log:", " | ".join(tail[-3:]))

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: the wipe works - input bit for bit on the left, the processed frame on the right")
    return 0


if __name__ == "__main__":
    sys.exit(main())
