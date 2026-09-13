"""A frame whose row pitch needs padding still comes back.

D3D12 pads every row of a copyable footprint to 256 bytes, so only widths that
are multiples of 64 come out unpadded. Every screen resolution is one, which is
why the worker's readback - which asked Map for RowPitch*rows, past the end of
the buffer - worked for years and then killed the worker (exit 9, nothing
logged) the moment one-window mode made arbitrary window widths normal.

So this test uses a deliberately awkward size: an odd width whose pitch is
padded, and an odd height for good measure.

Run:  python3 test_odd_frame_size.py
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
                  HEADER_FMT, OUT_FMT, OUT_MAGIC, PROFILES, VIDEO_MAGIC,
                  WORKER_EXE)

# 1002 * 4 = 4008 bytes per row, which D3D12 pads to 4096 - the case that used
# to fail. 517 rows, an odd number, so the last-row arithmetic is exercised too.
W, H = 1002, 517
WARMUP = 8
FRAMES = 4


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"the worker closed stdout ({len(buf)} of {n})")
        buf += chunk
    return buf


def main() -> int:
    if (skip := needs_worker()) is not None:
        return skip
    if not WORKER_EXE.is_file():
        print(f"FAIL: worker not found: {WORKER_EXE}")
        return 1
    pitch = W * 4
    padded = (pitch + 255) // 256 * 256
    print(f"{W}x{H}: row {pitch} bytes, padded to {padded} "
          f"({'padding needed' if padded != pitch else 'already aligned'})")
    if padded == pitch:
        print("FAIL: this size needs no padding - it does not test anything")
        return 1

    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, WARMUP, 0, 0, 0,
                         int(params.get("style", 0)),
                         int(params.get("auto_mask", 0)),
                         int(params.get("ui_correction", 0)),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)

    yy, xx = np.mgrid[0:H, 0:W]
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[..., 0] = ((xx * 7) % 256).astype(np.uint8)
    frame[..., 1] = ((yy * 5) % 256).astype(np.uint8)
    frame[..., 2] = 128
    frame[..., 3] = 255
    frame = np.ascontiguousarray(frame)
    motion = np.zeros((H, W, 2), dtype=np.float16)

    worker = subprocess.Popen([str(WORKER_EXE), "--live"],
                              cwd=str(WORKER_EXE.parent),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    failures = []
    last = None
    try:
        worker.stdin.write(header)
        worker.stdin.flush()
        for i in range(FRAMES):
            worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, i,
                                           1 if i == 0 else 0,
                                           FRAME_FLAG_WANT_PIXELS, i))
            worker.stdin.write(frame.tobytes())
            worker.stdin.write(motion.tobytes())
            worker.stdin.flush()
            head = read_exact(worker.stdout, struct.calcsize(OUT_FMT))
            magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
            if magic != OUT_MAGIC:
                failures.append(f"foreign reply 0x{magic:08X}")
                break
            if not ok:
                failures.append(f"frame {i}: ok=0, ngx=0x{ngx:08X}")
                break
            if nbytes != W * H * 4:
                failures.append(f"frame {i}: {nbytes} bytes, expected {W * H * 4}")
                break
            last = np.frombuffer(read_exact(worker.stdout, nbytes),
                                 dtype=np.uint8).reshape(H, W, 4)
    except EOFError as exc:
        failures.append(f"the worker died on a padded-pitch frame: {exc}")
    finally:
        try:
            worker.stdin.close()
        except Exception:
            pass
        worker.wait(timeout=10)
        if worker.returncode not in (0, None):
            failures.append(f"the worker exited with code {worker.returncode}")
        err = worker.stderr.read().decode("utf-8", "replace")
        tail = [l for l in err.splitlines() if "feature 18" in l or "failed" in l]
        if tail:
            print("worker log:", " | ".join(tail[-2:]))

    if last is not None:
        # The last column and the last row are where a wrong pitch shows up
        # first: they would come back black or shifted.
        edge = last[:, -1, :3].mean()
        bottom = last[-1, :, :3].mean()
        print(f"result {last.shape}, last column mean {edge:.0f}, "
              f"last row mean {bottom:.0f}")
        if edge < 8 or bottom < 8:
            failures.append("the last row/column came back black - the pitch is wrong")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: a padded row pitch goes through the pipeline intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
