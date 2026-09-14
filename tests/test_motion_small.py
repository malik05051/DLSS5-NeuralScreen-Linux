"""The downscaled motion field (MOTS): motion arrives at flow resolution
and the worker upscales it on the GPU.

The client negotiates a smaller motion size (e.g. 320x180 for a 1280x720
work frame); frames then carry FRAME_FLAG_MOTION_SMALL and the motion
payload is that smaller size. The worker must accept the negotiation,
ack it, and keep producing real frames.

Checks:
* the MACK ack comes back ok=1;
* frames with the small motion flag still come back at the full size;
* the frames are real pictures (the upscaled motion did not break NGX).
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

from main import (FRAME_FLAG_MOTION_SMALL, FRAME_FLAG_WANT_PIXELS,  # noqa: E402
                  FRAME_FMT, FRAME_MAGIC, HEADER_FMT, MOTION_ACK_FMT,
                  MOTION_ACK_MAGIC, MOTION_FMT, MOTION_MAGIC, OUT_FMT,
                  OUT_MAGIC, PROFILES, VIDEO_MAGIC, WORKER_EXE)

W, H = 1280, 720
WORK_W, WORK_H = 1280, 720
MOT_W, MOT_H = 320, 180
WARMUP = 8


def make_frame(seed: int) -> np.ndarray:
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


def send_mots(worker, w: int, h: int) -> int:
    worker.stdin.write(struct.pack(MOTION_FMT, MOTION_MAGIC, w, h, 0, 0))
    worker.stdin.flush()
    ack = read_exact(worker.stdout, struct.calcsize(MOTION_ACK_FMT))
    magic, ok, _r1, _r2, _pts = struct.unpack(MOTION_ACK_FMT, ack)
    if magic != MOTION_ACK_MAGIC:
        raise RuntimeError(f"foreign reply to MOTS: 0x{magic:08X}")
    return ok


def send_frame(worker, index: int, frame: np.ndarray, motion: np.ndarray,
               small: bool) -> np.ndarray:
    flags = FRAME_FLAG_WANT_PIXELS | (FRAME_FLAG_MOTION_SMALL if small else 0)
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                   1 if index == 0 else 0, flags, index))
    worker.stdin.write(frame.tobytes())
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()
    head = read_exact(worker.stdout, struct.calcsize(OUT_FMT))
    magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
    if magic != OUT_MAGIC or not ok:
        raise RuntimeError(f"bad reply magic=0x{magic:08X} ok={ok} ngx=0x{ngx:08X}")
    if nbytes == 0:
        raise RuntimeError("empty frame")
    return np.frombuffer(read_exact(worker.stdout, nbytes),
                         dtype=np.uint8).reshape(H, W, 4).copy()


def main() -> int:
    if (skip := needs_worker()) is not None:
        return skip
    failures = []
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, WORK_W, WORK_H, WARMUP, 0,
                         0, 0, int(params["style"]), int(params["auto_mask"]),
                         int(params["ui_correction"]),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)

    proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(WORKER_EXE.parent),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        proc.stdin.write(header)
        proc.stdin.flush()

        # 1. Negotiate the small motion size.
        ok = send_mots(proc, MOT_W, MOT_H)
        print(f"MOTS {MOT_W}x{MOT_H}: ok={ok}")
        if not ok:
            failures.append("the worker refused the small motion size")

        # 2. A frame with the small motion flag.
        frame = make_frame(3)
        motion = np.zeros((MOT_H, MOT_W, 2), dtype=np.float16)
        out = send_frame(proc, 0, frame, motion, small=True)
        if out.shape != (H, W, 4):
            failures.append(f"frame came back {out.shape}, expected {(H, W, 4)}")

        # 3. A frame WITHOUT the flag (motion at work size) - both must work.
        motion_full = np.zeros((WORK_H, WORK_W, 2), dtype=np.float16)
        out2 = send_frame(proc, 1, frame, motion_full, small=False)
        if out2.shape != (H, W, 4):
            failures.append(f"full-motion frame came back {out2.shape}")

        # 4. Real pictures, not blank.
        for name, o in (("small-motion", out), ("full-motion", out2)):
            g = o[..., :3].astype(np.float32).mean(axis=2)
            lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1]
                   + g[1:-1, :-2] + g[1:-1, 2:])
            d = float(lap.var())
            print(f"{name}: detail {d:.0f}")
            if d < 10:
                failures.append(f"{name} frame is flat ({d:.1f})")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the downscaled motion field is upscaled and the pipeline survives")
    return 0


if __name__ == "__main__":
    sys.exit(main())
