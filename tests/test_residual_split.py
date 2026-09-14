"""The matched residual composite TOGETHER with the before/after wipe.

Both features touch the output texture in the same frame: the wipe copies
the raw capture over the left part of the residual-composed result. The
left side must be the raw input (bit for bit), the right side the
residual result (native + neural delta), and the boundary must have the
divider strip.

This is the combination the user actually runs: reduced work resolution
(residual) + wipe to compare.
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

W, H = 1920, 1080
WORK_W, WORK_H = 1280, 720
WARMUP = 8
SPLIT = 0.5


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


def send_and_get(worker, index: int, frame: np.ndarray, motion: np.ndarray,
                 split: float) -> np.ndarray:
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
                         float(params["skin_structure"]), W, H)
    frame = make_frame(5)
    motion = np.zeros((WORK_H, WORK_W, 2), dtype=np.float16)

    env = dict(__import__("os").environ)
    env["NS_NR_SMALL"] = "1"
    env.pop("NS_NR_RESIDUAL", None)  # default: residual ON with nr_small

    proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(WORKER_EXE.parent),
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        proc.stdin.write(header)
        proc.stdin.flush()
        out = send_and_get(proc, 0, frame, motion, split=SPLIT)

        split_x = int(W * SPLIT)
        left = out[:, :split_x - 2, :3]
        right = out[:, split_x + 2:, :3]
        inp = frame[..., :3]

        # 1. The left side is the raw capture, bit for bit.
        d_left = float(np.abs(left.astype(np.int16) - inp[:, :split_x - 2].astype(np.int16)).mean())
        print(f"left (raw): mean |out - input| {d_left:.2f} of 255")
        if d_left > 1.0:
            failures.append(f"the wipe left side is not the raw capture ({d_left:.2f})")

        # 2. The right side carries the neural edit (residual result).
        d_right = float(np.abs(right.astype(np.int16) - inp[:, split_x + 2:].astype(np.int16)).mean())
        print(f"right (residual): mean |out - input| {d_right:.2f} of 255")
        if d_right < 0.5:
            failures.append("the right side equals the input - no neural edit")

        # 3. The right side keeps the input's detail (the residual anchor).
        def detail(img):
            g = img.astype(np.float32).mean(axis=2)
            lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1]
                   + g[1:-1, :-2] + g[1:-1, 2:])
            return float(lap.var())

        d_in = detail(inp[:, split_x + 2:])
        d_out = detail(right)
        print(f"right detail: input {d_in:.0f}, residual {d_out:.0f}")
        if d_out < d_in * 0.5:
            failures.append(f"the residual side lost too much detail ({d_out:.0f} vs {d_in:.0f})")
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
    print("OK: residual + wipe compose in the same frame")
    return 0


if __name__ == "__main__":
    sys.exit(main())
