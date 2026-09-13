"""The matched residual composite: the neural delta lands on the pristine
native frame instead of a stretched low-res upscale.

The worker is driven exactly like test_nr_small does it (--live, a fixed
pattern, FRAME_FLAG_WANT_PIXELS), with NS_NR_RESIDUAL on and off. What is
checked:

* residual mode returns a real full-size frame (not blank, not flat);
* it keeps MORE of the input's high frequencies than the plain upscale
  (the native 1:1 anchor - the whole point of the composite);
* it still carries the network's edit (it is not just the raw input);
* strength 0.0 degenerates to native + 0*edit = native, as the formula says.
"""
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from _needs import needs_worker  # noqa: E402

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

from main import (FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,  # noqa: E402
                  HEADER_FMT, NATIVE_DIR, OUT_FMT, OUT_MAGIC, PROFILES,
                  VIDEO_MAGIC, WORKER_EXE)

FULL_W, FULL_H = 1920, 1080
WORK_W, WORK_H = 1280, 720
WARMUP = 8
FRAMES = 3


def make_frame(w: int, h: int) -> np.ndarray:
    """Detail everywhere + a few sharp edges (text-like): on flat colour a
    broken composite looks just like a good one."""
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:h, 0:w]
    f = np.zeros((h, w, 4), dtype=np.uint8)
    base = ((xx * 7 + yy * 13) % 256).astype(np.uint8)
    f[..., 0] = base
    f[..., 1] = (base // 2 + rng.integers(0, 48, (h, w), dtype=np.uint8))
    f[..., 2] = (255 - base).astype(np.uint8)
    # A hard vertical edge - the composite must keep it as sharp as native.
    f[:, w // 3:w // 3 + 2] = 255
    f[..., 3] = 255
    return np.ascontiguousarray(f)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"worker closed stdout ({len(buf)} of {n})")
        buf += chunk
    return buf


def run_worker(residual: bool, strength: float = 1.0) -> dict:
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(
        HEADER_FMT, VIDEO_MAGIC, WORK_W, WORK_H, WARMUP, 0,
        0, 0, int(params["style"]), int(params["auto_mask"]),
        int(params["ui_correction"]),
        float(params["intensity"]), float(params["local_tone"]),
        float(params["local_structure"]), float(params["skin_structure"]),
        FULL_W, FULL_H)
    frame = make_frame(FULL_W, FULL_H)
    motion = np.zeros((WORK_H, WORK_W, 2), dtype=np.float16)
    body = frame.tobytes() + motion.tobytes()

    env = dict(os.environ)
    env["NS_NR_SMALL"] = "1"
    # Residual is ON by default with nr_small; the env only forces it off
    # (the plain upscale path) for the comparison run.
    env["NS_NR_RESIDUAL"] = "1" if residual else "0"
    env["NS_NR_RESIDUAL_STRENGTH"] = str(strength)
    proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(NATIVE_DIR),
                            env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = None
    try:
        proc.stdin.write(header)
        proc.stdin.flush()
        for i in range(FRAMES):
            proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, i,
                                         1 if i == 0 else 0,
                                         FRAME_FLAG_WANT_PIXELS, i))
            proc.stdin.write(body)
            proc.stdin.flush()
            head = read_exact(proc.stdout, struct.calcsize(OUT_FMT))
            magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
            if magic != OUT_MAGIC or not ok:
                raise RuntimeError(f"bad reply magic=0x{magic:08X} ok={ok} ngx=0x{ngx:08X}")
            if nbytes:
                data = read_exact(proc.stdout, nbytes)
                out = np.frombuffer(data, dtype=np.uint8).reshape(FULL_H, FULL_W, 4).copy()
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
    return {"out": out, "log": err, "input": frame}


def detail(img: np.ndarray) -> float:
    """Variance of the Laplacian - high frequencies, edges, texture."""
    g = img[..., :3].astype(np.float32).mean(axis=2)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1]
           + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def main() -> int:
    if (skip := needs_worker()) is not None:
        return skip
    failures = []

    plain = run_worker(residual=False)
    res = run_worker(residual=True)
    zero = run_worker(residual=True, strength=0.0)

    if plain["out"] is None or res["out"] is None or zero["out"] is None:
        failures.append("no pixels came back in one of the runs")
        for name, r in (("plain", plain), ("res", res), ("zero", zero)):
            if r["out"] is None:
                print(f"{name}: {[l for l in r['log'].splitlines() if '[nr]' in l or 'fail' in l.lower()][-3:]}")
        return 1

    inp = res["input"]
    d_in = detail(inp)
    d_plain = detail(plain["out"])
    d_res = detail(res["out"])
    d_zero = detail(zero["out"])
    print(f"detail: input {d_in:.0f}, plain upscale {d_plain:.0f}, residual {d_res:.0f}, residual@0 {d_zero:.0f}")

    if res["out"].shape != (FULL_H, FULL_W, 4):
        failures.append(f"residual returned {res['out'].shape}, expected the full size")

    # 1. Not blank.
    if d_res < d_in * 0.02:
        failures.append(f"the residual frame is flat ({d_res:.1f} vs {d_in:.1f} in the input)")

    # 2. The 1:1 native anchor keeps more detail than the stretched upscale.
    if d_res < d_plain * 1.02:
        failures.append(f"residual kept LESS high-frequency detail than plain "
                        f"upscale ({d_res:.1f} vs {d_plain:.1f}) - the composite "
                        f"did not anchor on the native frame")

    # 3. The network's edit is still there: residual != raw native, and the
    #    mean diff to the input stays in a sane band (not a duplicate, not noise).
    diff_res = float(np.abs(res["out"][..., :3].astype(np.int16)
                            - inp[..., :3].astype(np.int16)).mean())
    print(f"mean |residual - input|: {diff_res:.1f} of 255")
    if diff_res < 0.5:
        failures.append("residual equals the raw input - the network's edit is gone")
    if diff_res > 90:
        failures.append(f"residual is far from the input ({diff_res:.1f}) - nonsense frame")

    # 4. Strength 0 degenerates to the native frame.
    diff_zero = float(np.abs(zero["out"][..., :3].astype(np.int16)
                             - inp[..., :3].astype(np.int16)).mean())
    print(f"mean |residual@0 - input|: {diff_zero:.1f} of 255")
    if diff_zero > 12:
        failures.append(f"strength=0.0 must leave the native frame untouched "
                        f"(mean diff {diff_zero:.1f})")

    # 5. The edge at x=w/3 is sharper in residual mode (the anchor keeps it).
    def edge_sharpness(img: np.ndarray) -> float:
        """Mean |gradient| across the hard edge at x=w/3 (a few px window),
        averaged over the full height."""
        g = img[..., :3].astype(np.float32).mean(axis=2)
        x0 = FULL_W // 3
        w_ = 3
        left = g[:, x0 - w_:x0].mean(axis=1)
        right = g[:, x0:x0 + w_].mean(axis=1)
        return float(np.abs(right - left).mean())

    e_plain = edge_sharpness(plain["out"])
    e_res = edge_sharpness(res["out"])
    print(f"edge sharpness: plain {e_plain:.2f}, residual {e_res:.2f}")
    if e_res < e_plain * 1.05:
        failures.append(f"the hard edge is NOT sharper in residual mode "
                        f"({e_res:.2f} vs plain {e_plain:.2f})")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the residual composite anchors 1:1 detail and carries the neural edit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
