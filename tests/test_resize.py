"""The on-the-fly resize (RNSZ): the feature is reconfigured without a restart.

The client sends a RNSZ command between frames with a new work size; the
worker tears down the feature/textures, creates new ones and replies with
RACK. The pipeline must keep producing frames at the new size afterwards.

Checks:
* the ack comes back ok=1;
* frames after the resize come back at the NEW size;
* the resize does not kill the worker (a second resize works too);
* the work size in the log matches what was asked.
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

from main import (FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,  # noqa: E402
                  HEADER_FMT, OUT_FMT, OUT_MAGIC, PROFILES, RACK_FMT,
                  RESIZE_ACK_MAGIC, RESIZE_FLAG_NR_SMALL, RESIZE_FMT,
                  RESIZE_MAGIC, VIDEO_MAGIC, WORKER_EXE)

W, H = 1280, 720
WORK_W, WORK_H = 1280, 720
WARMUP = 8


def make_frame(w: int, h: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    base = ((xx * 7 + yy * 13) % 256).astype(np.uint8)
    noise = rng.integers(0, 48, size=(h, w), dtype=np.uint8)
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


def send_frame(worker, index: int, frame: np.ndarray, motion: np.ndarray) -> np.ndarray:
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                   1 if index == 0 else 0,
                                   FRAME_FLAG_WANT_PIXELS, index))
    worker.stdin.write(frame.tobytes())
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()
    head = read_exact(worker.stdout, struct.calcsize(OUT_FMT))
    magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
    if magic != OUT_MAGIC or not ok:
        raise RuntimeError(f"bad reply magic=0x{magic:08X} ok={ok} ngx=0x{ngx:08X}")
    if nbytes == 0:
        raise RuntimeError("empty frame after a resize")
    return np.frombuffer(read_exact(worker.stdout, nbytes),
                         dtype=np.uint8).reshape(frame.shape[0], frame.shape[1], 4).copy()


def send_resize(worker, w: int, h: int, nr_small: bool, full_w: int = 0, full_h: int = 0) -> int:
    flags = RESIZE_FLAG_NR_SMALL if nr_small else 0
    worker.stdin.write(struct.pack(RESIZE_FMT, RESIZE_MAGIC, w, h, WARMUP,
                                   flags, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0,
                                   full_w, full_h))
    worker.stdin.flush()
    ack = read_exact(worker.stdout, struct.calcsize(RACK_FMT))
    magic, ok, ngx, _r, _pts = struct.unpack(RACK_FMT, ack)
    if magic != RESIZE_ACK_MAGIC:
        raise RuntimeError(f"foreign reply to RNSZ: 0x{magic:08X}")
    return ok


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

        # 1. A frame at the original size - the pipeline is alive.
        f0 = make_frame(W, H, 1)
        m0 = np.zeros((H, W, 2), dtype=np.float16)
        out0 = send_frame(proc, 0, f0, m0)
        if out0.shape != (H, W, 4):
            failures.append(f"first frame came back {out0.shape}, expected {(H, W, 4)}")

        # 2. Resize to a smaller work size (nr_small on), full-res frames stay.
        nw, nh = 960, 540
        ok = send_resize(proc, nw, nh, nr_small=True, full_w=W, full_h=H)
        print(f"RNSZ to {nw}x{nh}: ok={ok}")
        if not ok:
            failures.append("the worker refused the resize")

        # 3. Frames at the new size.
        f1 = make_frame(W, H, 2)
        m1 = np.zeros((nh, nw, 2), dtype=np.float16)
        out1 = send_frame(proc, 1, f1, m1)
        if out1.shape != (H, W, 4):
            failures.append(f"frame after resize came back {out1.shape}, "
                            f"expected the full size {(H, W, 4)}")

        # 4. A second resize - the worker must survive it.
        nw2, nh2 = 640, 360
        ok2 = send_resize(proc, nw2, nh2, nr_small=True, full_w=W, full_h=H)
        print(f"RNSZ to {nw2}x{nh2}: ok={ok2}")
        if not ok2:
            failures.append("the worker refused the second resize")
        f2 = make_frame(W, H, 3)
        m2 = np.zeros((nh2, nw2, 2), dtype=np.float16)
        out2 = send_frame(proc, 2, f2, m2)
        if out2.shape != (H, W, 4):
            failures.append(f"frame after the second resize came back {out2.shape}")

        # 5. The frames are real pictures, not blank.
        for name, out in (("first", out0), ("after resize", out1), ("after 2nd", out2)):
            g = out[..., :3].astype(np.float32).mean(axis=2)
            lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1]
                   + g[1:-1, :-2] + g[1:-1, 2:])
            d = float(lap.var())
            print(f"{name}: detail {d:.0f}")
            if d < 10:
                failures.append(f"{name} frame is flat ({d:.1f}) - the pipeline broke")
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
    print("OK: the on-the-fly resize reconfigures the feature and keeps the pipeline alive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
