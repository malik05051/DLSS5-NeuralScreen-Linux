"""A worker that keeps dying is given up on, not restarted for ever.

The loop restarts the worker when it goes silent for 5 s, and counts the
restarts so that a worker which cannot be revived turns NR off instead of
being restarted indefinitely. Every restart is visible - the overlay has no
frame for the length of an NGX init - so "restart for ever" is a flicker
every few seconds for as long as the program runs.

The counter's reset is the whole difficulty. It has to exist, or three
unrelated failures an hour apart turn NR off; and it has to not fire on a
single frame, or a worker that hands over one frame and then stalls clears
the count on every cycle and the give-up threshold is never reached. That
was the bug (issue #9, reported on the Proton path): the program was
unusable because the failure it could not recover from was never reported.

The rule: the chain is broken once the revived worker has stayed up for
WORKER_HEALTHY_AFTER, and a frame before that does not break it.

This is arithmetic on a counter and a clock, so it is checked directly
rather than by running the loop - no worker, no GPU, no compositor.

Run:  python3 test_restart_chain.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import (MAX_CONSECUTIVE_RESTARTS,  # noqa: E402
                      WORKER_HEALTHY_AFTER)


class _State:
    """The three fields the restart chain reads."""

    def __init__(self):
        self.consecutive_restarts = 0
        self.last_worker_revive = 0.0
        self.nr_off = False


def _on_frame(st, now):
    """main's reset, as the recv path performs it."""
    if (st.consecutive_restarts
            and now - st.last_worker_revive >= WORKER_HEALTHY_AFTER):
        st.consecutive_restarts = 0


def _on_silence(st, now):
    """main's failure branch: count it, then give up or revive."""
    st.consecutive_restarts += 1
    if st.consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
        st.nr_off = True
        st.consecutive_restarts = 0
        return
    st.last_worker_revive = now


def main() -> int:
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: expected {want}, got {got}")

    # 1. The reported bug. The worker is revived, delivers a frame almost at
    #    once, then goes silent 5 s later - for ever. The chain has to reach
    #    the threshold and report, not cycle.
    st = _State()
    now = 0.0
    restarts = 0
    for _ in range(40):
        now += 0.1
        _on_frame(st, now)       # one frame, right after coming up
        now += 5.0               # then silence until the deadline
        _on_silence(st, now)
        restarts += 1
        if st.nr_off:
            break
    check("a one-frame-then-stall worker is given up on", st.nr_off, True)
    check("and it takes MAX_CONSECUTIVE_RESTARTS attempts to do it",
          restarts, MAX_CONSECUTIVE_RESTARTS)

    # 2. The reset still has to work, or the original complaint comes back:
    #    failures far apart must not accumulate. A worker that runs healthily
    #    between failures can fail any number of times.
    st = _State()
    now = 0.0
    for _ in range(10):
        now += 5.0
        _on_silence(st, now)                 # it fails
        now += WORKER_HEALTHY_AFTER + 1.0    # then runs well for a while
        _on_frame(st, now)
    check("failures spaced by a healthy run never turn NR off", st.nr_off,
          False)
    check("and leave no count behind", st.consecutive_restarts, 0)

    # 3. The boundary: a frame arriving just before the worker counts as
    #    healthy must not clear the count.
    st = _State()
    _on_silence(st, 5.0)
    check("one failure counted", st.consecutive_restarts, 1)
    _on_frame(st, 5.0 + WORKER_HEALTHY_AFTER - 0.01)
    check("a frame just inside the window keeps the count",
          st.consecutive_restarts, 1)
    _on_frame(st, 5.0 + WORKER_HEALTHY_AFTER)
    check("a frame at the window clears it", st.consecutive_restarts, 0)

    # 4. A healthy session never touches the counter at all.
    st = _State()
    for i in range(100):
        _on_frame(st, i * 0.016)
    check("a healthy session stays at zero", st.consecutive_restarts, 0)
    check("and never turns NR off", st.nr_off, False)

    for line in failures:
        print(f"FAIL: {line}")
    if failures:
        return 1
    print("OK: a dying worker is given up on after "
          f"{MAX_CONSECUTIVE_RESTARTS}, and a healthy run clears the chain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
