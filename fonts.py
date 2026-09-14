"""Which typefaces the overlay draws with.

Up to 1.6.0 every glyph in the program was Consolas - titles, labels,
hints, values alike. One monospaced face for a whole interface is why the
menu read as a debug console rather than as a program.

The split here is by ROLE, not by taste:

* proportional (Segoe UI) for language - titles, labels, hints, buttons;
* monospaced for READINGS - fps, resolutions, timers, key captions. A
  fixed advance keeps digits from dancing sideways as they change, which
  is the one thing Consolas was actually good for.

Faces are looked up by FILE, not by family name. pygame's SysFont
normalises "segoeui" to segoeuil.ttf - the Light weight, too thin over a
game - and does not resolve CascadiaMono.ttf at all. A `fonts/` directory
next to the program wins over the system ones, so a shipped face (IBM
Plex, say) replaces Segoe without touching this module: drop the files in
under the names listed below.

CJK is the exception that stays family-based: Chinese, Japanese and
Korean have no glyphs in the Latin faces (tofu boxes), and the system
fonts that do carry them differ per script.
"""
from __future__ import annotations

import os
from pathlib import Path

import pygame

from paths import BASE_DIR


# Shipped faces win over system ones - first name that exists is used. The
# fallbacks are the faces a Linux desktop actually has: DejaVu is on every
# distribution, Liberation and Noto on most.
UI_FACES = ("IBMPlexSans-Regular.ttf", "DejaVuSans.ttf",
            "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf")
UI_BOLD_FACES = ("IBMPlexSans-SemiBold.ttf", "DejaVuSans-Bold.ttf",
                 "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf")
MONO_FACES = ("IBMPlexMono-Regular.ttf", "DejaVuSansMono.ttf",
              "LiberationMono-Regular.ttf", "NotoSansMono-Regular.ttf")
MONO_BOLD_FACES = ("IBMPlexMono-Medium.ttf", "DejaVuSansMono-Bold.ttf",
                   "LiberationMono-Bold.ttf", "NotoSansMono-Bold.ttf")

# Where to look, in order: our own folder first, then the system trees. The
# directories are searched recursively because a distribution puts
# DejaVuSans.ttf under truetype/dejavu/ and Noto under truetype/noto/, and
# hard-coding either would work on exactly one family of distributions.
FONT_DIRS = (BASE_DIR / "fonts",
             Path.home() / ".local/share/fonts",
             Path.home() / ".fonts",
             Path("/usr/share/fonts"),
             Path("/usr/local/share/fonts"))

# Per-script system families, resolved through fontconfig. The Windows
# build named Yu Gothic, Malgun Gothic and YaHei; the Linux equivalents are
# the Noto CJK faces, which every distribution packages under one name and
# which cover all three scripts from one family.
CJK_FONTS = {"zh": "Noto Sans CJK SC", "ja": "Noto Sans CJK JP",
             "ko": "Noto Sans CJK KR"}

_cache: dict = {}


def _find(names) -> Path | None:
    """The first of `names` that exists, searched shallow then deep.

    Shallow first so our own fonts/ directory answers immediately; the
    recursive walk is only reached for a system face, and only once - the
    result is cached by the caller.
    """
    for directory in FONT_DIRS:
        for name in names:
            path = directory / name
            if path.exists():
                return path
    for directory in FONT_DIRS:
        if not directory.is_dir():
            continue
        for name in names:
            try:
                found = next(directory.rglob(name), None)
            except OSError:
                continue
            if found is not None:
                return found
    return None


def _fontconfig(family: str) -> Path | None:
    """Ask fontconfig where a family lives. None when it cannot say.

    Used for the CJK faces: their file names differ between distributions
    (NotoSansCJK-Regular.ttc, NotoSansCJKsc-Regular.otf, ...) while the
    family name does not, so the name is the thing to look up. fc-match is
    part of fontconfig, which anything with a font on it already has.
    """
    import shutil
    import subprocess

    binary = shutil.which("fc-match")
    if binary is None:
        return None
    try:
        out = subprocess.run([binary, "-f", "%{file}", family],
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    path = Path(out.stdout.strip()) if out.stdout.strip() else None
    return path if path is not None and path.exists() else None


def load(size: int, mono: bool = False, bold: bool = False,
         lang: str = "en"):
    """A font for `size` px. Cached - the loaders are called per redraw."""
    size = max(8, int(size))
    key = (size, mono, bold, lang if lang in CJK_FONTS else "")
    hit = _cache.get(key)
    if hit is not None:
        return hit
    font = _build(size, mono, bold, lang)
    _cache[key] = font
    return font


def _build(size: int, mono: bool, bold: bool, lang: str):
    if lang in CJK_FONTS:
        # The CJK families are only reachable by name - there is no stable
        # file name for them across distributions. fontconfig is asked
        # first because it knows the real file; SysFont is the fallback,
        # and it goes through fontconfig too, just with less control.
        path = _fontconfig(CJK_FONTS[lang])
        if path is not None:
            try:
                font = pygame.font.Font(str(path), size)
                if bold:
                    font.set_bold(True)
                return font
            except Exception:
                pass
        try:
            return pygame.font.SysFont(CJK_FONTS[lang], size, bold=bold)
        except Exception:
            pass
    faces = ((MONO_BOLD_FACES if bold else MONO_FACES) if mono
             else (UI_BOLD_FACES if bold else UI_FACES))
    path = _find(faces)
    if path is not None:
        try:
            font = pygame.font.Font(str(path), size)
            # A family with no separate bold file resolves to the regular
            # one. Synthesise the weight rather than draw a heading in the
            # body face - the menu's hierarchy is carried by it.
            if bold and path == _find(MONO_FACES if mono else UI_FACES):
                font.set_bold(True)
            return font
        except Exception:
            pass
    # Nothing found on disk: let fontconfig pick something, and pygame's
    # own bundled face if even that fails. A menu in the wrong typeface is
    # a menu; no menu is a bug report.
    try:
        return pygame.font.SysFont("monospace" if mono else "sans", size,
                                   bold=bold)
    except Exception:
        return pygame.font.Font(None, size)


def clear_cache() -> None:
    """Drop the cached faces - used by the tests and after a scale change."""
    _cache.clear()
