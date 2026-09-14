"""The overlay's pixels reach the compositor in the format it reads.

Three things the Wayland port has to get right before anything is visible,
and all three are checkable without a compositor:

  * **The channel order.** wl_shm's ARGB8888 is a 32-bit little-endian word,
    so the bytes in memory run B, G, R, A. The drawing surface is built with
    matching masks so handing it over is a memcpy - if the masks are wrong
    the picture is not subtly off, it is blue where it should be red, and
    every 4K frame pays for a shuffle that should not exist.

  * **Premultiplied alpha.** The compositor treats ARGB8888 as
    premultiplied. Straight alpha makes every half-transparent pixel of the
    menu glow; the opaque case must be left alone, because it is the common
    one and it is on the frame path.

  * **The global alpha.** It stands in for the layered window's LWA_ALPHA,
    which was the only way Windows could make a whole window see-through.
    It has to compose with the per-pixel alpha rather than replace it.

Run:  python3 test_wayland_surface.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pygame  # noqa: E402

import wayland_shell  # noqa: E402


def main() -> int:
    pygame.init()
    failures = []

    # 1. The masks: a known colour must land as B, G, R, A in memory.
    surface = wayland_shell.make_surface(4, 2)
    surface.fill((10, 20, 30, 255))
    raw = np.frombuffer(surface.get_buffer().raw, dtype=np.uint8)
    if raw.size != 4 * 2 * 4:
        failures.append(f"the surface is {raw.size} bytes, expected 32 - "
                        "the rows are padded and present() cannot memcpy")
    pixel = list(raw.reshape(2, 4, 4)[0, 0])
    if pixel != [30, 20, 10, 255]:
        failures.append(f"the first pixel is {pixel}, expected [30, 20, 10, 255] "
                        "(B, G, R, A) - the surface masks do not match "
                        "wl_shm ARGB8888")

    # 2. An opaque buffer is left bit for bit alone.
    opaque = np.array([[[30, 20, 10, 255]]], dtype=np.uint8)
    before = opaque.copy()
    wayland_shell.premultiply(opaque)
    if not np.array_equal(opaque, before):
        failures.append("premultiply touched a fully opaque buffer - that is "
                        "the frame path and it must cost nothing")

    # 3. A half-transparent pixel is folded toward zero, alpha untouched.
    blended = np.array([[[200, 100, 50, 128]]], dtype=np.uint8)
    wayland_shell.premultiply(blended)
    got = list(blended[0, 0])
    if got[3] != 128:
        failures.append(f"premultiply changed the alpha: {got}")
    for i, (channel, expected) in enumerate(zip(got[:3], (100, 50, 25))):
        # (value * 128 + 128) >> 8, within one of half the value.
        if abs(int(channel) - expected) > 1:
            failures.append(f"channel {i} premultiplied to {channel}, "
                            f"expected about {expected}")

    # 4. Every pygame cursor the menu asks for has a Wayland shape. A
    #    missing one is a pointer that silently keeps the last shape while
    #    the user drags a panel edge.
    for name in ("SYSTEM_CURSOR_ARROW", "SYSTEM_CURSOR_SIZEALL",
                 "SYSTEM_CURSOR_SIZEWE", "SYSTEM_CURSOR_SIZENS",
                 "SYSTEM_CURSOR_HAND"):
        cursor = getattr(pygame, name)
        if cursor not in wayland_shell.CURSOR_SHAPES:
            failures.append(f"{name} has no cursor-shape mapping")

    # 5. The keysym table covers what the menu actually reads: Escape, the
    #    arrows, Enter, and the numpad with Num Lock both ways. The last one
    #    is the point of the port - on Windows the numpad with Num Lock off
    #    was indistinguishable from the navigation keys.
    table = wayland_shell.KEYSYM_TO_PYGAME
    for keysym, want, label in (
        (0xFF1B, pygame.K_ESCAPE, "Escape"),
        (0xFF52, pygame.K_UP, "Up"),
        (0xFF0D, pygame.K_RETURN, "Return"),
        (0xFFB1, pygame.K_KP1, "KP_1 (Num Lock on)"),
        (0xFF9C, pygame.K_KP1, "KP_End (Num Lock off)"),
    ):
        if table.get(keysym) != want:
            failures.append(f"keysym {label} maps to {table.get(keysym)}, "
                            f"expected {want}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: ARGB masks, premultiply, cursor shapes and the keysym table")
    return 0


if __name__ == "__main__":
    sys.exit(main())
