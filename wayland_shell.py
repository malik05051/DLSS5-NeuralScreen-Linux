"""The overlay's window, as Wayland understands one.

On Windows the overlay was an ordinary top-level window bent into shape with
four flags: WS_EX_TOPMOST to sit above everything, WS_EX_TRANSPARENT plus
WS_EX_LAYERED to let clicks fall through, WS_EX_NOACTIVATE to keep the
keyboard where it was, and SetWindowDisplayAffinity to hide it from other
programs' screen capture. None of those exist here, and three of the four
have exact Wayland counterparts that are not flags but protocol:

  WS_EX_TOPMOST       -> zwlr_layer_shell_v1, layer = overlay
  WS_EX_TRANSPARENT   -> wl_surface.set_input_region(an empty region)
  WS_EX_NOACTIVATE    -> keyboard_interactivity = none
  (the menu, open)    -> keyboard_interactivity = exclusive, input region = full

The fourth has no counterpart and cannot have one: a Wayland client cannot
ask the compositor to hide it from another client, because a Wayland client
cannot see another client in the first place. That turns out not to matter -
the reason WDA_EXCLUDEFROMCAPTURE existed was to stop the overlay from being
captured by our own Desktop Duplication, and the portal hands us a stream
that the compositor composes; see capture.py, which asks the portal to leave
us out of it by capturing a single output rather than the whole compositor.

Rendering is wl_shm with three buffers in rotation. pygame draws into an
ordinary offscreen Surface exactly as it did before - the whole menu in
overlay_ui.py is untouched - and `present()` copies it into whichever slot
the compositor is not holding. The surface is built with ARGB channel masks
so that copy is a memcpy and nothing shuffles 33 MB of 4K pixels per frame.
A frame that finds all three slots still in flight is dropped rather than
waited for: stalling the loop on a compositor's release is how a 60 fps
pipeline becomes a 30 fps one, and the user is looking at the screen.

Compositors without layer-shell (GNOME, as of Mutter's standing refusal to
implement it) get an xdg-shell fullscreen surface instead. It is an honestly
worse overlay - it takes focus, it cannot be click-through in the same way,
and it sits above the game only while the game is not fullscreen - and the
program says so in the log rather than pretending the two are equivalent.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import mmap
import os
import sys
import tempfile
import threading
import time

import numpy as np
import pygame

import wlproto

from pywayland.client import Display as WlDisplay
from pywayland.protocol.wayland import (WlCompositor, WlOutput, WlSeat, WlShm,
                                        WlSurface)
from pywayland.protocol.xdg_shell import XdgWmBase
from pywayland.protocol.xdg_output_unstable_v1 import ZxdgOutputManagerV1

try:
    from pywayland.protocol.cursor_shape_v1 import WpCursorShapeManagerV1
except ImportError:      # pywayland older than the protocol
    WpCursorShapeManagerV1 = None


# wl_shm ARGB8888 is a 32-bit little-endian word, so the bytes in memory run
# B, G, R, A. Premultiplied, which is what the surface compositing expects.
FORMAT_ARGB = WlShm.format.argb8888.value
FORMAT_XRGB = WlShm.format.xrgb8888.value


# ---------------------------------------------------------------------------
# xkbcommon - turning a keycode into something the menu can use
# ---------------------------------------------------------------------------


class _Xkb:
    """Just enough libxkbcommon to read the compositor's keymap.

    Wayland hands a client the keymap as a file descriptor and expects it to
    be interpreted; there is no "what character is this key" call in the
    protocol. Without xkbcommon a Dvorak or an AZERTY user would rebind a
    hotkey and get the QWERTY letter printed on nobody's keyboard.

    Missing libxkbcommon is survivable: the fallback maps the handful of
    keycodes the menu actually needs (arrows, Enter, Escape) from the evdev
    numbers, which are layout-independent anyway.
    """

    def __init__(self):
        self.lib = None
        self._ctx = None
        self._keymap = None
        self._state = None
        for name in ("libxkbcommon.so.0", "libxkbcommon.so",
                     ctypes.util.find_library("xkbcommon")):
            if not name:
                continue
            try:
                self.lib = ctypes.CDLL(name)
                break
            except OSError:
                continue
        if self.lib is None:
            print("[wayland] libxkbcommon is missing - keyboard layout "
                  "handling falls back to evdev keycodes", file=sys.stderr)
            return
        lib = self.lib
        lib.xkb_context_new.restype = ctypes.c_void_p
        lib.xkb_context_new.argtypes = [ctypes.c_int]
        lib.xkb_keymap_new_from_string.restype = ctypes.c_void_p
        lib.xkb_keymap_new_from_string.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        lib.xkb_state_new.restype = ctypes.c_void_p
        lib.xkb_state_new.argtypes = [ctypes.c_void_p]
        lib.xkb_state_key_get_one_sym.restype = ctypes.c_uint32
        lib.xkb_state_key_get_one_sym.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.xkb_state_key_get_utf8.restype = ctypes.c_int
        lib.xkb_state_key_get_utf8.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]
        lib.xkb_state_update_mask.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        lib.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
        lib.xkb_state_unref.argtypes = [ctypes.c_void_p]
        self._ctx = ctypes.c_void_p(lib.xkb_context_new(0))

    @property
    def ready(self) -> bool:
        return self._state is not None

    def load(self, fd: int, size: int) -> None:
        """Take the keymap the compositor sent (format 1: text v1)."""
        try:
            with mmap.mmap(fd, size, mmap.MAP_PRIVATE, mmap.PROT_READ) as mm:
                text = mm.read(size).split(b"\0", 1)[0]
        except Exception as exc:
            print(f"[wayland] could not read the keymap: {exc}", file=sys.stderr)
            return
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        if self.lib is None or self._ctx is None:
            return
        keymap = self.lib.xkb_keymap_new_from_string(self._ctx, text, 1, 0)
        if not keymap:
            return
        state = self.lib.xkb_state_new(ctypes.c_void_p(keymap))
        if not state:
            self.lib.xkb_keymap_unref(ctypes.c_void_p(keymap))
            return
        self._release()
        self._keymap = ctypes.c_void_p(keymap)
        self._state = ctypes.c_void_p(state)

    def update_mask(self, depressed: int, latched: int, locked: int,
                    group: int) -> None:
        if self._state is None or self.lib is None:
            return
        self.lib.xkb_state_update_mask(self._state, depressed, latched, locked,
                                       0, 0, group)

    def keysym(self, keycode: int) -> int:
        """The keysym for an evdev keycode. 0 when there is no keymap.

        Wayland reports evdev codes; xkb numbers keys eight higher, a
        constant that has outlived the hardware reason for it.
        """
        if self._state is None or self.lib is None:
            return 0
        return int(self.lib.xkb_state_key_get_one_sym(self._state, keycode + 8))

    def text(self, keycode: int) -> str:
        if self._state is None or self.lib is None:
            return ""
        buf = ctypes.create_string_buffer(16)
        n = self.lib.xkb_state_key_get_utf8(self._state, keycode + 8, buf, 16)
        if n <= 0:
            return ""
        return buf.raw[:n].decode("utf-8", "replace")

    def _release(self) -> None:
        if self.lib is None:
            return
        if self._state is not None:
            self.lib.xkb_state_unref(self._state)
            self._state = None
        if self._keymap is not None:
            self.lib.xkb_keymap_unref(self._keymap)
            self._keymap = None

    def close(self) -> None:
        self._release()


#: X keysym -> pygame key. Only what the menu reads: the arrows and Enter for
#: the choice lists, Escape everywhere, and enough of the rest that a rebind
#: dialog can name the key the user pressed.
KEYSYM_TO_PYGAME = {
    0xFF1B: pygame.K_ESCAPE, 0xFF0D: pygame.K_RETURN, 0xFF8D: pygame.K_KP_ENTER,
    0xFF09: pygame.K_TAB, 0xFF08: pygame.K_BACKSPACE, 0xFFFF: pygame.K_DELETE,
    0xFF52: pygame.K_UP, 0xFF54: pygame.K_DOWN, 0xFF51: pygame.K_LEFT,
    0xFF53: pygame.K_RIGHT, 0xFF50: pygame.K_HOME, 0xFF57: pygame.K_END,
    0xFF55: pygame.K_PAGEUP, 0xFF56: pygame.K_PAGEDOWN, 0xFF63: pygame.K_INSERT,
    0xFF7F: pygame.K_NUMLOCK, 0x0020: pygame.K_SPACE,
    # The numpad, with Num Lock on (the digits) and off (the navigation
    # keysyms). Both map to the same pygame key on purpose: unlike Windows,
    # where the numpad was unusable with Num Lock off because the codes were
    # literally the navigation keys, here the two are distinguishable and
    # Num1 means Num1 either way. That is one whole paragraph of the README
    # that this port gets to delete.
    0xFFB0: pygame.K_KP0, 0xFFB1: pygame.K_KP1, 0xFFB2: pygame.K_KP2,
    0xFFB3: pygame.K_KP3, 0xFFB4: pygame.K_KP4, 0xFFB5: pygame.K_KP5,
    0xFFB6: pygame.K_KP6, 0xFFB7: pygame.K_KP7, 0xFFB8: pygame.K_KP8,
    0xFFB9: pygame.K_KP9, 0xFFAE: pygame.K_KP_PERIOD, 0xFFAB: pygame.K_KP_PLUS,
    0xFFAD: pygame.K_KP_MINUS, 0xFFAA: pygame.K_KP_MULTIPLY,
    0xFFAF: pygame.K_KP_DIVIDE,
    0xFF9E: pygame.K_KP0, 0xFF9C: pygame.K_KP1, 0xFF99: pygame.K_KP2,
    0xFF9B: pygame.K_KP3, 0xFF96: pygame.K_KP4, 0xFF9D: pygame.K_KP5,
    0xFF98: pygame.K_KP6, 0xFF95: pygame.K_KP7, 0xFF97: pygame.K_KP8,
    0xFF9A: pygame.K_KP9, 0xFF9F: pygame.K_KP_PERIOD,
    0xFFE1: pygame.K_LSHIFT, 0xFFE2: pygame.K_RSHIFT,
    0xFFE3: pygame.K_LCTRL, 0xFFE4: pygame.K_RCTRL,
    0xFFE9: pygame.K_LALT, 0xFFEA: pygame.K_RALT,
    0xFFEB: pygame.K_LMETA, 0xFFEC: pygame.K_RMETA,
}
for _n in range(12):
    KEYSYM_TO_PYGAME[0xFFBE + _n] = pygame.K_F1 + _n
for _c in range(ord("a"), ord("z") + 1):
    KEYSYM_TO_PYGAME[_c] = getattr(pygame, f"K_{chr(_c)}")
    KEYSYM_TO_PYGAME[_c - 32] = getattr(pygame, f"K_{chr(_c)}")
for _d in range(10):
    KEYSYM_TO_PYGAME[0x30 + _d] = getattr(pygame, f"K_{_d}")

#: Evdev keycodes, for the machine with no libxkbcommon. Layout-independent
#: by construction: these are positions on the keyboard, not letters.
EVDEV_TO_PYGAME = {
    1: pygame.K_ESCAPE, 28: pygame.K_RETURN, 96: pygame.K_KP_ENTER,
    103: pygame.K_UP, 108: pygame.K_DOWN, 105: pygame.K_LEFT,
    106: pygame.K_RIGHT, 15: pygame.K_TAB, 14: pygame.K_BACKSPACE,
    57: pygame.K_SPACE, 110: pygame.K_INSERT, 111: pygame.K_DELETE,
    102: pygame.K_HOME, 107: pygame.K_END, 104: pygame.K_PAGEUP,
    109: pygame.K_PAGEDOWN, 69: pygame.K_NUMLOCK,
    82: pygame.K_KP0, 79: pygame.K_KP1, 80: pygame.K_KP2, 81: pygame.K_KP3,
    75: pygame.K_KP4, 76: pygame.K_KP5, 77: pygame.K_KP6, 71: pygame.K_KP7,
    72: pygame.K_KP8, 73: pygame.K_KP9, 83: pygame.K_KP_PERIOD,
    78: pygame.K_KP_PLUS, 74: pygame.K_KP_MINUS, 55: pygame.K_KP_MULTIPLY,
    98: pygame.K_KP_DIVIDE,
    42: pygame.K_LSHIFT, 54: pygame.K_RSHIFT, 29: pygame.K_LCTRL,
    97: pygame.K_RCTRL, 56: pygame.K_LALT, 100: pygame.K_RALT,
}
for _n in range(10):
    EVDEV_TO_PYGAME[59 + _n] = pygame.K_F1 + _n
EVDEV_TO_PYGAME[87] = pygame.K_F11
EVDEV_TO_PYGAME[88] = pygame.K_F12

#: pygame's system cursors -> the names in the cursor-shape protocol. The
#: menu asks for a move cursor while a panel is being dragged and for the
#: resize arrows on its edges, and those are exactly the shapes a compositor
#: already has for its own window decorations.
CURSOR_SHAPES = {
    pygame.SYSTEM_CURSOR_ARROW: "default",
    pygame.SYSTEM_CURSOR_HAND: "pointer",
    pygame.SYSTEM_CURSOR_SIZEALL: "move",
    pygame.SYSTEM_CURSOR_SIZEWE: "ew_resize",
    pygame.SYSTEM_CURSOR_SIZENS: "ns_resize",
    pygame.SYSTEM_CURSOR_SIZENWSE: "nwse_resize",
    pygame.SYSTEM_CURSOR_SIZENESW: "nesw_resize",
    pygame.SYSTEM_CURSOR_IBEAM: "text",
    pygame.SYSTEM_CURSOR_WAIT: "wait",
    pygame.SYSTEM_CURSOR_NO: "not_allowed",
}

#: Which bit of the xkb modifier mask is which. Set by the compositor's
#: keymap, read here to build pygame's KMOD_* so overlay_ui.key_text can
#: spell "Ctrl+Alt+Q" without asking SDL, which is not running.
MOD_SHIFT, MOD_CTRL, MOD_ALT, MOD_LOGO = 1, 4, 8, 64


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class Output:
    """One monitor, as the compositor describes it.

    The identity that travels into the config is `name` - "DP-1", "eDP-1" -
    which is what Wayland, the portal, and every desktop's display settings
    all agree on. It replaces the DXGI '\\\\.\\DISPLAY1' the Windows build
    saved, and is better in the one way that matters: it survives a reorder,
    because it names the connector rather than a position in a list.
    """

    def __init__(self, proxy, global_name: int):
        self.proxy = proxy
        self.global_name = global_name
        self.name = ""
        self.description = ""
        self.x, self.y = 0, 0            # position, in logical pixels
        self.width, self.height = 0, 0   # size, in physical pixels
        self.logical_w, self.logical_h = 0, 0
        self.scale = 1
        self.transform = 0
        self.refresh = 0

    @property
    def rotated(self) -> bool:
        """90 or 270 degrees - the sides are swapped relative to the mode."""
        return self.transform in (1, 3, 5, 7)

    def __repr__(self) -> str:
        return (f"<Output {self.name or '?'} {self.width}x{self.height}"
                f"@{self.x},{self.y} scale {self.scale}>")


# ---------------------------------------------------------------------------
# Shared-memory buffers
# ---------------------------------------------------------------------------


class _BufferPool:
    """wl_shm buffers for one surface size, recycled on release.

    One anonymous file holds all the slots; the compositor maps it once and
    each wl_buffer is a window into it. Resizing throws the whole pool away -
    a 4K pool is 33 MB per slot and keeping the old one around to be tidy
    costs more than recreating it.
    """

    SLOTS = 3

    def __init__(self, shm, width: int, height: int, opaque: bool):
        self.width, self.height = width, height
        self.stride = width * 4
        self.slot_size = self.stride * height
        self.total = self.slot_size * self.SLOTS
        fd, path = tempfile.mkstemp(prefix="neuralscreen-shm-",
                                    dir=os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        try:
            os.unlink(path)     # the fd is the only handle from here on
            os.ftruncate(fd, self.total)
            self._map = mmap.mmap(fd, self.total)
            self._pool = shm.create_pool(fd, self.total)
        finally:
            os.close(fd)
        fmt = FORMAT_XRGB if opaque else FORMAT_ARGB
        self._buffers = []
        self._busy = []
        for i in range(self.SLOTS):
            buf = self._pool.create_buffer(i * self.slot_size, width, height,
                                           self.stride, fmt)
            buf.dispatcher["release"] = self._released
            self._buffers.append(buf)
            self._busy.append(False)
        self._view = np.frombuffer(self._map, dtype=np.uint8)

    def _released(self, buf) -> None:
        for i, candidate in enumerate(self._buffers):
            if candidate is buf:
                self._busy[i] = False
                return

    def take(self):
        """(index, a (h, w, 4) uint8 view), or (None, None) when all are busy."""
        for i, busy in enumerate(self._busy):
            if not busy:
                start = i * self.slot_size
                view = self._view[start:start + self.slot_size].reshape(
                    self.height, self.width, 4)
                return i, view
        return None, None

    def buffer(self, index: int):
        self._busy[index] = True
        return self._buffers[index]

    def close(self) -> None:
        for buf in self._buffers:
            try:
                buf.destroy()
            except Exception:
                pass
        self._buffers.clear()
        try:
            self._pool.destroy()
        except Exception:
            pass
        self._view = None
        try:
            self._map.close()
        except (BufferError, ValueError):
            pass


#: The channel masks that make a pygame surface byte-identical to wl_shm's
#: ARGB8888. A surface built with these needs no conversion on the way out -
#: `present()` is a memcpy, and a 4K frame is 33 MB that does not get
#: shuffled 60 times a second.
ARGB_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)


def make_surface(width: int, height: int) -> pygame.Surface:
    """A drawing surface laid out the way the compositor reads it."""
    return pygame.Surface((int(width), int(height)), pygame.SRCALPHA, 32,
                          ARGB_MASKS)


def premultiply(argb: np.ndarray) -> None:
    """Premultiply an ARGB8888 buffer in place.

    The compositor treats ARGB8888 as premultiplied, and handing it straight
    alpha makes every half-transparent pixel of the menu glow. The multiply
    is in uint16 and shifted rather than divided - this runs on 8.3 million
    pixels per 4K frame, and a divide there is measurable.

    A fully opaque buffer is left alone, which is the common case: the frame
    from the worker has alpha 255 everywhere and only the menu on top of it
    does not.
    """
    alpha = argb[:, :, 3]
    if int(alpha.min()) == 255:
        return
    a16 = alpha.astype(np.uint16)
    for channel in (0, 1, 2):
        argb[:, :, channel] = (
            (argb[:, :, channel].astype(np.uint16) * a16 + 128) >> 8
        ).astype(np.uint8)


# ---------------------------------------------------------------------------
# The connection
# ---------------------------------------------------------------------------


class WaylandShell:
    """The compositor connection and the globals everything else needs.

    One per process. `connect()` returns False rather than raising when
    there is no compositor - the program can still be asked for `--version`
    or run its own tests on a machine with no display.
    """

    def __init__(self):
        self.display = None
        self.compositor = None
        self.shm = None
        self.seat = None
        self.layer_shell = None
        self.layer_surface_cls = None
        self.layer_shell_cls = None
        self.xdg_wm_base = None
        self.xdg_output_manager = None
        self.cursor_shape = None
        self.cursor_device = None
        self.outputs: dict[int, Output] = {}
        self.xkb = _Xkb()
        self.serial = 0
        self._pointer = None
        self._keyboard = None
        self._lock = threading.RLock()
        #: Surfaces that want input, by their wl_surface proxy.
        self._surfaces: dict = {}
        self._focus = None
        self._pointer_pos = (0, 0)
        self._mods = 0
        self._pending_outputs = 0

    # -- setup -------------------------------------------------------------

    def connect(self) -> bool:
        # One connection per process: a Display rebuilt on a monitor switch
        # reuses it. pump() drops self.display when the compositor is gone,
        # and that is the one case a second connect actually connects.
        if self.display is not None:
            return True
        if os.environ.get("WAYLAND_DISPLAY", "") == "" and \
                os.environ.get("WAYLAND_SOCKET", "") == "":
            print("[wayland] WAYLAND_DISPLAY is not set - this build needs a "
                  "Wayland session", file=sys.stderr)
            return False
        try:
            self.display = WlDisplay()
            self.display.connect()
        except Exception as exc:
            print(f"[wayland] could not connect: {exc}", file=sys.stderr)
            self.display = None
            return False
        # Everything bound belongs to a connection. A second connect - the
        # Display is rebuilt on a monitor switch, and again after a drop -
        # must not carry the old one's proxies into the new registry walk:
        # KWin advertises zxdg_output_manager_v1 before wl_output, and
        # get_xdg_output on a stale output is "invalid arguments", which
        # kills the new connection and starts the cycle over (15 fps with
        # the overlay reconnecting every two seconds).
        self.compositor = self.shm = self.seat = None
        self.layer_shell = self.layer_surface_cls = self.layer_shell_cls = None
        self.xdg_wm_base = self.xdg_output_manager = None
        self.cursor_shape = self.cursor_device = None
        self._pointer = self._keyboard = None
        self.outputs = {}
        self._surfaces = {}
        self._focus = None
        registry = self.display.get_registry()
        registry.dispatcher["global"] = self._on_global
        registry.dispatcher["global_remove"] = self._on_global_remove
        self.display.roundtrip()     # the globals
        self.display.roundtrip()     # their events (output modes, xdg_output)
        if self.compositor is None or self.shm is None:
            print("[wayland] the compositor offers no wl_compositor/wl_shm",
                  file=sys.stderr)
            return False
        if self.layer_shell is None:
            why = wlproto.reason() or "the compositor does not offer it"
            print(f"[wayland] no wlr-layer-shell ({why}) - the overlay falls "
                  "back to an xdg-shell window: it takes focus, it cannot be "
                  "click-through, and it will not stay above a fullscreen "
                  "game", file=sys.stderr)
        return True

    def _on_global(self, registry, name, interface, version) -> None:
        try:
            if interface == "wl_compositor":
                self.compositor = registry.bind(name, WlCompositor,
                                                min(version, 4))
            elif interface == "wl_shm":
                self.shm = registry.bind(name, WlShm, 1)
            elif interface == "wl_seat":
                self.seat = registry.bind(name, WlSeat, min(version, 7))
                self.seat.dispatcher["capabilities"] = self._on_seat_caps
            elif interface == "wl_output":
                proxy = registry.bind(name, WlOutput, min(version, 4))
                output = Output(proxy, name)
                self.outputs[name] = output
                proxy.dispatcher["geometry"] = self._on_output_geometry
                proxy.dispatcher["mode"] = self._on_output_mode
                proxy.dispatcher["scale"] = self._on_output_scale
                proxy.dispatcher["name"] = self._on_output_name
                proxy.dispatcher["description"] = self._on_output_description
                self._attach_xdg_output(output)
            elif interface == "xdg_wm_base":
                self.xdg_wm_base = registry.bind(name, XdgWmBase,
                                                 min(version, 3))
                self.xdg_wm_base.dispatcher["ping"] = \
                    lambda base, serial: base.pong(serial)
            elif interface == "zxdg_output_manager_v1":
                self.xdg_output_manager = registry.bind(
                    name, ZxdgOutputManagerV1, min(version, 3))
                for output in self.outputs.values():
                    self._attach_xdg_output(output)
            elif interface == "wp_cursor_shape_manager_v1" \
                    and WpCursorShapeManagerV1 is not None:
                self.cursor_shape = registry.bind(name, WpCursorShapeManagerV1,
                                                  min(version, 1))
            elif interface == "zwlr_layer_shell_v1":
                pair = wlproto.layer_shell()
                if pair is None:
                    return
                shell_cls, surface_cls = pair
                self.layer_shell = registry.bind(name, shell_cls,
                                                 min(version, 4))
                self.layer_surface_cls = surface_cls
                self.layer_shell_cls = shell_cls
        except Exception as exc:
            print(f"[wayland] could not bind {interface}: {exc}",
                  file=sys.stderr)

    def _on_global_remove(self, registry, name) -> None:
        self.outputs.pop(name, None)

    def _attach_xdg_output(self, output: Output) -> None:
        """Ask for the logical geometry - position and size after scaling.

        wl_output's own numbers are the mode: a 4K monitor at 200% reports
        3840x2160 and a position that means nothing on a compositor that
        does not use a global coordinate space. xdg_output is the one that
        answers in the coordinates the portal's stream and the pointer are
        expressed in, which is what the overlay has to be placed in.
        """
        if self.xdg_output_manager is None:
            return
        try:
            xdg = self.xdg_output_manager.get_xdg_output(output.proxy)
        except Exception:
            return

        def _position(_proxy, x, y):
            output.x, output.y = int(x), int(y)

        def _size(_proxy, w, h):
            output.logical_w, output.logical_h = int(w), int(h)

        def _name(_proxy, name):
            if not output.name:
                output.name = str(name)

        xdg.dispatcher["logical_position"] = _position
        xdg.dispatcher["logical_size"] = _size
        xdg.dispatcher["name"] = _name

    # -- output events -----------------------------------------------------

    def _on_output_geometry(self, proxy, x, y, pw, ph, subpixel, make, model,
                            transform) -> None:
        out = self._output_of(proxy)
        if out is None:
            return
        out.transform = int(transform)
        if not out.description:
            out.description = f"{make} {model}".strip()

    def _on_output_mode(self, proxy, flags, width, height, refresh) -> None:
        out = self._output_of(proxy)
        # bit 0 is WL_OUTPUT_MODE_CURRENT; the others are modes the monitor
        # merely supports, and taking one of those for the resolution is how
        # the capture ends up configured for a mode nobody is running.
        if out is None or not (flags & 1):
            return
        out.width, out.height = int(width), int(height)
        out.refresh = int(refresh)

    def _on_output_scale(self, proxy, factor) -> None:
        out = self._output_of(proxy)
        if out is not None:
            out.scale = int(factor) or 1

    def _on_output_name(self, proxy, name) -> None:
        out = self._output_of(proxy)
        if out is not None:
            out.name = str(name)

    def _on_output_description(self, proxy, description) -> None:
        out = self._output_of(proxy)
        if out is not None:
            out.description = str(description)

    def _output_of(self, proxy) -> Output | None:
        for out in self.outputs.values():
            if out.proxy is proxy:
                return out
        return None

    def output_by_name(self, name: str) -> Output | None:
        for out in self.outputs.values():
            if out.name == name:
                return out
        return None

    def output_list(self) -> list[Output]:
        """Outputs in a stable order: by position, then by name.

        Stable matters because the index is what the menu shows and what an
        old config may still hold. Sorting by the corner puts the leftmost
        screen first, which is also how people describe their monitors.
        """
        return sorted(self.outputs.values(), key=lambda o: (o.x, o.y, o.name))

    # -- seat --------------------------------------------------------------

    def _on_seat_caps(self, seat, capabilities) -> None:
        if capabilities & WlSeat.capability.pointer.value and self._pointer is None:
            self._pointer = seat.get_pointer()
            self._pointer.dispatcher["enter"] = self._ptr_enter
            self._pointer.dispatcher["leave"] = self._ptr_leave
            self._pointer.dispatcher["motion"] = self._ptr_motion
            self._pointer.dispatcher["button"] = self._ptr_button
            self._pointer.dispatcher["axis"] = self._ptr_axis
            if self.cursor_shape is not None:
                try:
                    self.cursor_device = self.cursor_shape.get_pointer(
                        self._pointer)
                except Exception:
                    self.cursor_device = None
        if capabilities & WlSeat.capability.keyboard.value and self._keyboard is None:
            self._keyboard = seat.get_keyboard()
            self._keyboard.dispatcher["keymap"] = self._kbd_keymap
            self._keyboard.dispatcher["enter"] = self._kbd_enter
            self._keyboard.dispatcher["leave"] = self._kbd_leave
            self._keyboard.dispatcher["key"] = self._kbd_key
            self._keyboard.dispatcher["modifiers"] = self._kbd_modifiers

    def register_surface(self, surface, overlay) -> None:
        self._surfaces[surface] = overlay

    def unregister_surface(self, surface) -> None:
        self._surfaces.pop(surface, None)

    def _target(self, surface):
        return self._surfaces.get(surface)

    # -- pointer -----------------------------------------------------------

    def _ptr_enter(self, pointer, serial, surface, x, y) -> None:
        self.serial = serial
        self._pointer_pos = (int(x), int(y))
        overlay = self._target(surface)
        if overlay is not None:
            overlay._pointer_in = True
            # The compositor does not draw a cursor for a surface that has
            # not set one. The menu is a pointer interface; without this the
            # user drags an invisible mouse.
            overlay._set_cursor(pointer, serial)

    def _ptr_leave(self, pointer, serial, surface) -> None:
        self.serial = serial
        overlay = self._target(surface)
        if overlay is not None:
            overlay._pointer_in = False

    def _ptr_motion(self, pointer, time_ms, x, y) -> None:
        prev = self._pointer_pos
        self._pointer_pos = (int(x), int(y))
        for overlay in self._surfaces.values():
            if overlay._pointer_in:
                overlay._push(pygame.event.Event(
                    pygame.MOUSEMOTION, pos=self._pointer_pos,
                    rel=(self._pointer_pos[0] - prev[0],
                         self._pointer_pos[1] - prev[1]),
                    buttons=(0, 0, 0), touch=False))

    def _ptr_button(self, pointer, serial, time_ms, button, state) -> None:
        self.serial = serial
        # evdev: BTN_LEFT 0x110, BTN_RIGHT 0x111, BTN_MIDDLE 0x112. pygame
        # numbers them 1, 3, 2 - the middle and right are the other way
        # round, which is the kind of thing that silently swaps a context
        # menu for a paste.
        index = {0x110: 1, 0x111: 3, 0x112: 2}.get(button, 0)
        if not index:
            return
        kind = pygame.MOUSEBUTTONDOWN if state else pygame.MOUSEBUTTONUP
        for overlay in self._surfaces.values():
            if overlay._pointer_in:
                overlay._push(pygame.event.Event(
                    kind, pos=self._pointer_pos, button=index, touch=False))

    def _ptr_axis(self, pointer, time_ms, axis, value) -> None:
        if axis != 0:       # 0 is vertical scroll; horizontal is ignored
            return
        # wl_fixed, and positive is down - the opposite of pygame's wheel.
        steps = -float(value) / 256.0 / 10.0
        for overlay in self._surfaces.values():
            if overlay._pointer_in:
                overlay._push(pygame.event.Event(
                    pygame.MOUSEWHEEL, x=0, y=int(round(steps)) or
                    (1 if steps > 0 else -1), flipped=False,
                    precise_x=0.0, precise_y=steps, touch=False))

    # -- keyboard ----------------------------------------------------------

    def _kbd_keymap(self, keyboard, fmt, fd, size) -> None:
        if fmt == 1:
            self.xkb.load(int(fd), int(size))
        else:
            try:
                os.close(int(fd))
            except OSError:
                pass

    def _kbd_enter(self, keyboard, serial, surface, keys) -> None:
        self.serial = serial
        self._focus = surface

    def _kbd_leave(self, keyboard, serial, surface) -> None:
        self.serial = serial
        if self._focus is surface:
            self._focus = None

    def _kbd_modifiers(self, keyboard, serial, depressed, latched, locked,
                       group) -> None:
        self.serial = serial
        self.xkb.update_mask(depressed, latched, locked, group)
        mods = 0
        if depressed & MOD_SHIFT:
            mods |= pygame.KMOD_SHIFT
        if depressed & MOD_CTRL:
            mods |= pygame.KMOD_CTRL
        if depressed & MOD_ALT:
            mods |= pygame.KMOD_ALT
        if depressed & MOD_LOGO:
            mods |= pygame.KMOD_META
        self._mods = mods

    def _kbd_key(self, keyboard, serial, time_ms, key, state) -> None:
        self.serial = serial
        overlay = self._target(self._focus) if self._focus is not None else None
        if overlay is None:
            return
        keysym = self.xkb.keysym(int(key)) if self.xkb.ready else 0
        pg_key = KEYSYM_TO_PYGAME.get(keysym)
        if pg_key is None:
            pg_key = EVDEV_TO_PYGAME.get(int(key), 0)
        text = self.xkb.text(int(key)) if self.xkb.ready else ""
        kind = pygame.KEYDOWN if state else pygame.KEYUP
        overlay._push(pygame.event.Event(
            kind, key=pg_key, mod=self._mods, unicode=text,
            scancode=int(key) + 8))

    # -- the loop ----------------------------------------------------------

    def pump(self) -> bool:
        """Read whatever the compositor has to say. False when it is gone.

        Non-blocking: the program's loop is driven by frames from the
        worker, not by the compositor, and a dispatch that waited here would
        stall the pipeline every time the user stopped moving the mouse.
        """
        if self.display is None:
            return False
        try:
            self.display.flush()
            self.display.dispatch(block=False)
            return True
        except Exception as exc:
            print(f"[wayland] the connection dropped: {exc}", file=sys.stderr)
            # Disconnect what is left rather than just forgetting it: a
            # wl_display freed by the garbage collector at interpreter exit
            # is a segfault (pywayland 0.4.19), and Ctrl+C after a drop was
            # exactly that.
            try:
                self.display.disconnect()
            except Exception:
                pass
            self.display = None
            return False

    def mods(self) -> int:
        return self._mods

    def close(self) -> None:
        self.xkb.close()
        if self.display is not None:
            try:
                self.display.disconnect()
            except Exception:
                pass
            self.display = None


#: The process-wide connection, opened by whoever needs it first.
SHELL = WaylandShell()


# ---------------------------------------------------------------------------
# The overlay surface
# ---------------------------------------------------------------------------


class Overlay:
    """One always-on-top surface, with pygame drawing into it.

    Two modes, matching what the Windows overlay did with SetWindowPos:

      * fullscreen - anchored to all four edges of one output, which is how
        layer-shell says "this is the size of that screen" without us having
        to know the screen's size or chase it when it changes;
      * window-follow - a fixed size placed by margins from the top-left
        corner, which is the closest the protocol comes to "put this
        rectangle there" and is used to track a captured window.
    """

    def __init__(self, shell: WaylandShell, width: int, height: int,
                 output: Output | None = None, click_through: bool = True):
        self._shell = shell
        self.width, self.height = int(width), int(height)
        self.output = output
        self._events: list = []
        self._events_lock = threading.Lock()
        self._pointer_in = False
        self._pool: _BufferPool | None = None
        self._configured = False
        self._closed = False
        self._visible = False
        self._click_through = click_through
        self._keyboard = False
        self._layer_surface = None
        self._xdg_surface = None
        self._xdg_toplevel = None
        self._cursor_surface = None
        self._cursor_shape = "default"
        self._pending_size: tuple[int, int] | None = None

        self.surface = shell.compositor.create_surface()
        shell.register_surface(self.surface, self)
        if shell.layer_shell is not None:
            self._make_layer_surface()
        else:
            self._make_xdg_surface()
        self._apply_input_region()
        self.surface.commit()
        shell.display.roundtrip()

    # -- construction ------------------------------------------------------

    def _make_layer_surface(self) -> None:
        shell = self._shell
        cls = shell.layer_surface_cls
        # The layer enum belongs to the shell's interface class. pywayland
        # 0.4.19 stopped forwarding enums through the proxy, which is why
        # this reads it from the class that was bound, not the binding.
        layer_enum = shell.layer_shell_cls.layer if shell.layer_shell_cls \
            else shell.layer_shell.layer
        self._layer_surface = shell.layer_shell.get_layer_surface(
            self.surface, self.output.proxy if self.output else None,
            layer_enum.overlay.value, "neuralscreen")
        ls = self._layer_surface
        ls.set_size(self.width, self.height)
        # Anchoring to all four edges makes the compositor configure us with
        # the output's own size, which is the size the capture will be too.
        anchor = (cls.anchor.top.value | cls.anchor.bottom.value
                  | cls.anchor.left.value | cls.anchor.right.value)
        ls.set_anchor(anchor)
        # -1: do not reserve any space and do not be pushed around by panels
        # that do. An overlay that shortened the desktop by its own height
        # every launch would be a memorable bug.
        ls.set_exclusive_zone(-1)
        ls.set_keyboard_interactivity(cls.keyboard_interactivity.none.value)
        ls.dispatcher["configure"] = self._layer_configure
        ls.dispatcher["closed"] = self._layer_closed

    def _layer_configure(self, ls, serial, width, height) -> None:
        ls.ack_configure(serial)
        if width and height:
            self._pending_size = (int(width), int(height))
        self._configured = True

    def _layer_closed(self, ls) -> None:
        self._closed = True
        self._push(pygame.event.Event(pygame.QUIT))

    def _make_xdg_surface(self) -> None:
        """The fallback: a plain fullscreen toplevel.

        Everything this cannot do is listed in the module docstring. It is
        here so that a GNOME user gets a working program rather than a
        message telling them to change compositor.
        """
        shell = self._shell
        if shell.xdg_wm_base is None:
            raise RuntimeError("the compositor offers neither layer-shell nor "
                               "xdg-shell - there is no way to put a window up")
        self._xdg_surface = shell.xdg_wm_base.get_xdg_surface(self.surface)
        self._xdg_surface.dispatcher["configure"] = self._xdg_configure
        self._xdg_toplevel = self._xdg_surface.get_toplevel()
        self._xdg_toplevel.set_title("NeuralScreen")
        self._xdg_toplevel.set_app_id("neuralscreen")
        self._xdg_toplevel.dispatcher["configure"] = self._xdg_toplevel_configure
        self._xdg_toplevel.dispatcher["close"] = self._layer_closed
        self._xdg_toplevel.set_fullscreen(
            self.output.proxy if self.output else None)

    def _xdg_configure(self, xdg_surface, serial) -> None:
        xdg_surface.ack_configure(serial)
        self._configured = True

    def _xdg_toplevel_configure(self, toplevel, width, height, states) -> None:
        if width and height:
            self._pending_size = (int(width), int(height))

    # -- input region ------------------------------------------------------

    def _apply_input_region(self) -> None:
        """Click-through, as Wayland spells it.

        An empty region means no part of this surface accepts input, so
        every click lands on whatever is underneath - the exact behaviour
        WS_EX_TRANSPARENT gave, and without the WS_EX_LAYERED trick it
        needed on Windows. Passing None instead would mean the opposite:
        NULL is "the whole surface", the protocol's default.
        """
        if self._click_through:
            region = self._shell.compositor.create_region()   # empty
            self.surface.set_input_region(region)
            region.destroy()
        else:
            self.surface.set_input_region(None)

    def set_click_through(self, enabled: bool) -> None:
        if enabled == self._click_through:
            return
        self._click_through = enabled
        self._apply_input_region()
        self.surface.commit()

    def set_keyboard(self, enabled: bool) -> None:
        """Take the keyboard while the menu is open, give it back after.

        `exclusive` rather than `on_demand`: the menu is opened by a global
        hotkey while the focus is on a game, and on_demand would leave the
        keys going to the game until the user clicked the overlay - which
        they cannot do, because until they click they have no pointer in it
        either.
        """
        if self._layer_surface is None or enabled == self._keyboard:
            return
        cls = self._shell.layer_surface_cls
        mode = (cls.keyboard_interactivity.exclusive if enabled
                else cls.keyboard_interactivity.none)
        self._layer_surface.set_keyboard_interactivity(mode.value)
        self.surface.commit()
        self._keyboard = enabled

    def set_cursor_shape(self, pygame_cursor: int) -> str:
        """Remember which shape the pointer should take over this surface.

        Applied on the next pointer enter, and immediately when the pointer
        is already inside. Returns the shape name that was chosen, which is
        what the tests assert on - there is no way to read a cursor back out
        of a compositor.
        """
        name = CURSOR_SHAPES.get(pygame_cursor, "default")
        self._cursor_shape = name
        if self._pointer_in and not self._click_through:
            self._apply_cursor_shape(self._shell.serial)
        return name

    def _apply_cursor_shape(self, serial: int) -> None:
        device = self._shell.cursor_device
        if device is None:
            return
        try:
            from pywayland.protocol.cursor_shape_v1 import WpCursorShapeDeviceV1

            shape = getattr(WpCursorShapeDeviceV1.shape, self._cursor_shape,
                            None)
            if shape is None:
                shape = WpCursorShapeDeviceV1.shape.default
            device.set_shape(serial, shape.value)
        except Exception:
            pass

    def _set_cursor(self, pointer, serial) -> None:
        """Give the pointer something to be while it is over the menu.

        The Windows build had the opposite problem - it drew its own cursor
        because a fullscreen game hid the system one - and that whole
        workaround is gone: here the compositor owns the pointer and a
        surface simply says what it should look like. An empty surface
        (no buffer) hides it, which is what click-through wants; the menu
        sets the default arrow through the cursor-shape protocol when the
        compositor has it, and otherwise leaves the pointer alone rather
        than loading a cursor theme by hand.
        """
        if self._click_through:
            if self._cursor_surface is None:
                self._cursor_surface = self._shell.compositor.create_surface()
            try:
                pointer.set_cursor(serial, self._cursor_surface, 0, 0)
            except Exception:
                pass
            return
        # No cursor-shape protocol means the compositor keeps whatever the
        # pointer was over last; _apply_cursor_shape returns quietly. Loading
        # an X cursor theme out of a .so to do better is not worth the
        # dependency for a menu.
        self._apply_cursor_shape(serial)

    # -- geometry ----------------------------------------------------------

    def set_output(self, output: Output | None) -> None:
        """Move the overlay to another monitor.

        layer-shell has no "move to output": the output is fixed when the
        layer surface is created. So the surface is rebuilt - which is
        cheap, and honest about what is happening, where SetWindowPos across
        monitors on Windows silently kept a window sized for the old one.
        """
        if output is self.output:
            return
        self.output = output
        self._teardown_role()
        if self._shell.layer_shell is not None:
            self._make_layer_surface()
        else:
            self._make_xdg_surface()
        self._apply_input_region()
        self.surface.commit()
        self._shell.display.roundtrip()

    def set_rect(self, x: int, y: int, w: int, h: int) -> None:
        """Place a fixed-size surface at a position, for window-follow mode.

        Anchored top-left and offset by margins: layer-shell's way of
        expressing a position. A compositor is free to ignore it, and some
        do for surfaces larger than the output, which is why the size is
        clamped here rather than argued about later.
        """
        if self._layer_surface is None:
            return
        cls = self._shell.layer_surface_cls
        w = max(1, int(w))
        h = max(1, int(h))
        self._layer_surface.set_anchor(cls.anchor.top.value
                                       | cls.anchor.left.value)
        self._layer_surface.set_size(w, h)
        self._layer_surface.set_margin(int(y), 0, 0, int(x))
        self.surface.commit()
        if (w, h) != (self.width, self.height):
            self.resize(w, h)

    def set_fullscreen(self) -> None:
        if self._layer_surface is None:
            return
        cls = self._shell.layer_surface_cls
        self._layer_surface.set_anchor(
            cls.anchor.top.value | cls.anchor.bottom.value
            | cls.anchor.left.value | cls.anchor.right.value)
        self._layer_surface.set_margin(0, 0, 0, 0)
        self.surface.commit()

    def resize(self, w: int, h: int) -> None:
        w, h = max(1, int(w)), max(1, int(h))
        if (w, h) == (self.width, self.height):
            return
        self.width, self.height = w, h
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        if self._layer_surface is not None:
            self._layer_surface.set_size(w, h)

    def pending_size(self) -> tuple[int, int] | None:
        """The size the compositor last configured, if it differs from ours.

        The compositor is the authority: a layer surface anchored to four
        edges is told the output's size, and a monitor switched from 1440p
        to 4K under a running pipeline announces itself here first.
        """
        size = self._pending_size
        if size is None or size == (self.width, self.height):
            return None
        return size

    # -- presentation ------------------------------------------------------

    def set_visible(self, visible: bool) -> None:
        """Show or hide, without destroying anything.

        Hiding is attaching no buffer: the surface stays, the compositor
        stops drawing it. That is the Wayland equivalent of ShowWindow(SW_HIDE)
        and it keeps the overlay's position, size and role across the NGX
        warm-up, when the Windows build had to create the window hidden and
        reveal it to avoid a blank flash.
        """
        if visible == self._visible:
            return
        self._visible = visible
        if not visible:
            self.surface.attach(None, 0, 0)
            self.surface.commit()

    @property
    def visible(self) -> bool:
        return self._visible

    def present(self, frame: pygame.Surface, opaque: bool = False,
                global_alpha: int = 255) -> bool:
        """Put a pygame surface on the screen. False when it was dropped.

        A dropped frame is not an error and not worth a log line: it means
        all three buffers are still with the compositor, which happens when
        the compositor is busier than we are. The alternative is waiting for
        a release, which turns somebody else's hitch into ours.
        """
        if self._closed or not self._visible:
            return False
        if self._pool is None or self._pool.width != self.width \
                or self._pool.height != self.height:
            if self._pool is not None:
                self._pool.close()
            self._pool = _BufferPool(self._shell.shm, self.width, self.height,
                                     opaque)
        index, view = self._pool.take()
        if index is None:
            return False
        # The surface is built with ARGB_MASKS, so its own bytes already are
        # what wl_shm wants: one copy, no channel shuffle. get_buffer() is a
        # view of the surface's pixels, not a conversion.
        src = np.frombuffer(frame.get_buffer().raw, dtype=np.uint8)
        expected = self.width * self.height * 4
        if src.size != expected:
            # The pygame surface and the wl_surface disagree about the size -
            # a resize is in flight. Skip this frame; the next one is built
            # for the new size.
            return False
        np.copyto(view, src.reshape(self.height, self.width, 4))
        if opaque:
            # Nothing to blend: fill the alpha in rather than trust the
            # drawing code to have, and skip the premultiply entirely.
            view[:, :, 3] = 255
        else:
            if global_alpha < 255:
                # The layer's own translucency, which on Windows was the
                # layered window's LWA_ALPHA - one number for the whole
                # window, and the only way it could be see-through. Here it
                # scales the alpha channel before the premultiply, so the
                # two compose correctly instead of fighting.
                view[:, :, 3] = ((view[:, :, 3].astype(np.uint16)
                                  * int(global_alpha)) >> 8).astype(np.uint8)
            premultiply(view)
        buf = self._pool.buffer(index)
        self.surface.attach(buf, 0, 0)
        self.surface.damage_buffer(0, 0, self.width, self.height)
        self.surface.commit()
        return True

    # -- events ------------------------------------------------------------

    def _push(self, event) -> None:
        with self._events_lock:
            # A pointer that moved twenty times while the pipeline was busy
            # does not need twenty events; the menu only ever reads the
            # latest position. Everything else is kept - a dropped click is
            # a button that does not work.
            if event.type == pygame.MOUSEMOTION and self._events \
                    and self._events[-1].type == pygame.MOUSEMOTION:
                self._events[-1] = event
                return
            self._events.append(event)

    def poll(self) -> list:
        """Everything that happened since the last call, as pygame events."""
        with self._events_lock:
            events, self._events = self._events, []
        return events

    # -- teardown ----------------------------------------------------------

    def _teardown_role(self) -> None:
        for proxy in (self._layer_surface, self._xdg_toplevel,
                      self._xdg_surface):
            if proxy is not None:
                try:
                    proxy.destroy()
                except Exception:
                    pass
        self._layer_surface = None
        self._xdg_toplevel = None
        self._xdg_surface = None
        self._configured = False

    def close(self) -> None:
        self._closed = True
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        self._teardown_role()
        if self._cursor_surface is not None:
            try:
                self._cursor_surface.destroy()
            except Exception:
                pass
            self._cursor_surface = None
        self._shell.unregister_surface(self.surface)
        try:
            self.surface.destroy()
        except Exception:
            pass
