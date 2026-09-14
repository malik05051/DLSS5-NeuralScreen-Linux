"""Result pixels through shared memory (OUTS) match the ones from the pipe.

A recorded frame at 4K weighs 33 MB and pushing it through the pipe cost ~7 ms.
The OUTS channel puts the same bytes into a section. This test drives the worker
directly and demands that one and the same input frame produce a BYTE FOR BYTE
identical result over both paths - otherwise the "optimisation" would quietly
corrupt the recording.

Switching back is checked too: after an OUTS with zero sizes the pixels must
travel inline through the pipe again.
"""
import mmap
import os
import struct
import subprocess
import sys
import uuid
from pathlib import Path

import numpy as np

from _needs import needs_worker  # noqa: E402

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)
from main import (FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,  # noqa: E402
                  HEADER_FMT, OUT_BYTES_IN_SHM, OUT_FMT, OUT_MAGIC,
                  OUTS_ACK_FMT, OUTS_ACK_MAGIC, OUTS_FMT, OUTS_MAGIC,
                  PROFILES, VIDEO_MAGIC, WORKER_EXE)

W, H = 1280, 720
WARMUP = 8


def make_frame(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    base = ((xx * 5 + yy * 11) % 256).astype(np.uint8)
    frame[..., 0] = base
    frame[..., 1] = (base // 2 + rng.integers(0, 40, (H, W), dtype=np.uint8))
    frame[..., 2] = (255 - base).astype(np.uint8)
    frame[..., 3] = 255
    return np.ascontiguousarray(frame)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"the worker closed stdout ({len(buf)} of {n})")
        buf += chunk
    return buf


def send_frame(worker, index: int, frame: np.ndarray, motion: np.ndarray,
               reset: int = 0):
    """reset=1 clears the temporal history of the model.

    Without the reset the same input gives a DIFFERENT output: feature 18
    accumulates history between frames. Two frames can only be compared after
    a reset.
    """
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                   1 if (index == 0 or reset) else 0,
                                   FRAME_FLAG_WANT_PIXELS, index))
    worker.stdin.write(frame.tobytes())
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()


def recv_result(worker, view):
    """Return (pixels, source) - 'shm' or 'pipe'."""
    head = read_exact(worker.stdout, struct.calcsize(OUT_FMT))
    magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
    assert magic == OUT_MAGIC, f"foreign reply 0x{magic:08X}"
    assert ok, f"ok=0, ngx=0x{ngx:08X}"
    if nbytes == OUT_BYTES_IN_SHM:
        return np.array(view, copy=True), "shm"
    data = read_exact(worker.stdout, nbytes)
    return np.frombuffer(data, dtype=np.uint8).reshape(H, W, 4).copy(), "pipe"


def main() -> int:
    if (skip := needs_worker()) is not None:
        return skip
    if not WORKER_EXE.is_file():
        print(f"FAIL: worker not found: {WORKER_EXE}")
        return 1
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, WARMUP, 0, 0, 0,
                         int(params.get("style", 0)),
                         int(params.get("auto_mask", 0)),
                         int(params.get("ui_correction", 0)),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)

    name = f"NeuralScreenTestOut_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    # The layout matches main.py: a seqlock in the first 8 bytes, then the
    # frame. The worker refuses a section with no room for both.
    mm = mmap.mmap(-1, W * H * 4 + 8, tagname=name)
    view = np.ndarray((H, W, 4), dtype=np.uint8, buffer=mm, offset=8)

    worker = subprocess.Popen([str(WORKER_EXE), "--live"],
                              cwd=str(WORKER_EXE.parent),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    failures = []
    try:
        worker.stdin.write(header)
        worker.stdin.flush()
        motion = np.zeros((H, W, 2), dtype=np.float16)
        frame = make_frame(7)

        # 1. The pipe path - the reference
        pipe_px, src = None, None
        for i in range(2):
            send_frame(worker, i, frame, motion)
            pipe_px, src = recv_result(worker, view)
        # The reference is taken with the history reset, otherwise there is
        # nothing to compare against
        send_frame(worker, 2, frame, motion, reset=1)
        pipe_px, src = recv_result(worker, view)
        print(f"without OUTS: the pixels arrived through {src}")
        if src != "pipe":
            failures.append(f"before the handshake the pixels went through {src}")

        # 2. Agree on OUTS
        worker.stdin.write(struct.pack(OUTS_FMT, OUTS_MAGIC, W, H, 0, 0,
                                       name.encode("ascii")))
        worker.stdin.flush()
        ack = read_exact(worker.stdout, struct.calcsize(OUTS_ACK_FMT))
        magic, ok, _r0, _r1, _pts = struct.unpack(OUTS_ACK_FMT, ack)
        print(f"OUTS: magic 0x{magic:08X}, ok={ok}")
        if magic != OUTS_ACK_MAGIC or not ok:
            print("FAIL: the worker did not accept OUTS")
            return 1

        # 3. The same frame - now through the section, and it must match
        send_frame(worker, 3, frame, motion, reset=1)
        shm_px, src = recv_result(worker, view)
        print(f"with OUTS: the pixels arrived through {src}")
        if src != "shm":
            failures.append("after the handshake the pixels still go down the pipe")
        elif pipe_px is not None:
            same = bool(np.array_equal(shm_px, pipe_px))
            diff = int((shm_px != pipe_px).sum())
            print(f"match with the pipe frame: "
                  f"{'bit for bit' if same else f'MISMATCH, {diff} bytes'}")
            if not same:
                failures.append(f"the frame through the section differs ({diff} bytes)")

        # 4. Turning the channel off - back to the pipe
        worker.stdin.write(struct.pack(OUTS_FMT, OUTS_MAGIC, 0, 0, 0, 0,
                                       name.encode("ascii")))
        worker.stdin.flush()
        ack = read_exact(worker.stdout, struct.calcsize(OUTS_ACK_FMT))
        _m, ok, _r0, _r1, _p = struct.unpack(OUTS_ACK_FMT, ack)
        send_frame(worker, 4, frame, motion, reset=1)
        back_px, src = recv_result(worker, view)
        print(f"after switching off: the pixels came through {src}")
        if src != "pipe":
            failures.append("the channel did not switch off, the pixels are still in the section")
        elif not np.array_equal(back_px, shm_px):
            failures.append("the frame after switching off did not match the previous one")
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
        for line in err.splitlines():
            if "outs" in line.lower():
                print("worker log:", line.strip())
        view = None
        mm.close()

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: the OUTS channel hands back the same bytes and switches off correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
