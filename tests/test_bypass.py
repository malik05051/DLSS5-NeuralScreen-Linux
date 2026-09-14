"""The bypass path (NR OFF): the worker skips NGX and shows the raw capture.

Driven directly, like test_split: we send the colour ourselves, so the
"raw capture" is exactly our input frame. With FRAME_FLAG_BYPASS the
output must equal the input bit for bit (no NGX ran), and the pipeline
must stay alive - the next non-bypass frame resumes the network.

Checks:
* bypass frame == input (bit for bit, or within rounding of the swizzle);
* a bypass frame does not kill the worker (the next NR frame still comes);
* the bypass flag is per-frame: NR ON after bypass works.
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

from main import (FRAME_FLAG_BYPASS, FRAME_FLAG_WANT_PIXELS, FRAME_FMT,  # noqa: E402
                  FRAME_MAGIC, HEADER_FMT, OUT_FMT, OUT_MAGIC, PROFILES,
                  VIDEO_MAGIC, WORKER_EXE)

W, H = 1280, 720
WORK_W, WORK_H = 1280, 720  # 1:1 - the raw capture is exactly the input
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


def send_and_get(worker, index: int, frame: np.ndarray, motion: np.ndarray,
                 bypass: bool) -> np.ndarray | None:
    flags = FRAME_FLAG_WANT_PIXELS | (FRAME_FLAG_BYPASS if bypass else 0)
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
    failures = []
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, WORK_W, WORK_H, WARMUP, 0,
                         0, 0, int(params["style"]), int(params["auto_mask"]),
                         int(params["ui_correction"]),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)
    frame = make_frame(11)
    motion = np.zeros((WORK_H, WORK_W, 2), dtype=np.float16)

    proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(WORKER_EXE.parent),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        proc.stdin.write(header)
        proc.stdin.flush()

        # 1. A normal NR frame first - the pipeline must be alive.
        nr = send_and_get(proc, 0, frame, motion, bypass=False)
        if nr is None:
            failures.append("no NR frame came back before the bypass test")
        else:
            diff = float(np.abs(nr[..., :3].astype(np.int16)
                                - frame[..., :3].astype(np.int16)).mean())
            print(f"NR frame: mean |out - input| {diff:.1f} of 255")
            if diff < 0.5:
                failures.append("the NR frame equals the input - the network did not run")

        # 2. The bypass frame: must be the raw capture, bit for bit.
        bp = send_and_get(proc, 1, frame, motion, bypass=True)
        if bp is None:
            failures.append("no bypass frame came back")
        else:
            diff = float(np.abs(bp[..., :3].astype(np.int16)
                                - frame[..., :3].astype(np.int16)).mean())
            print(f"bypass frame: mean |out - input| {diff:.1f} of 255")
            if diff > 1.0:
                failures.append(f"the bypass frame is not the raw capture "
                                f"(mean diff {diff:.1f}) - NGX ran or the "
                                f"frame is wrong")

        # 3. NR again after the bypass - the pipeline must have survived.
        nr2 = send_and_get(proc, 2, frame, motion, bypass=False)
        if nr2 is None:
            failures.append("no NR frame after the bypass - the worker died")
        else:
            diff = float(np.abs(nr2[..., :3].astype(np.int16)
                                - frame[..., :3].astype(np.int16)).mean())
            print(f"NR after bypass: mean |out - input| {diff:.1f} of 255")
            if diff < 0.5:
                failures.append("NR after the bypass equals the input - the "
                                "network did not resume")
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
    print("OK: bypass shows the raw capture and the pipeline survives it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
