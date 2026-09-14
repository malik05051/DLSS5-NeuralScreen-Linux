"""Save As and the folder picker, through the FileChooser portal.

On Windows these were GetSaveFileNameW and SHBrowseForFolder: a program
drew the system's own dialog inside its own process, parented to its own
window. On Wayland a client does not draw system dialogs and does not have
a window handle to parent one to - the desktop draws the chooser, in its own
process, and hands back a path. Which is why the `parent_hwnd` argument is
gone from these signatures rather than ignored: a parameter that does
nothing is worse than one that is not there.

The calls block, and they block for as long as it takes: a person deciding
where a screenshot goes is not on a computer's schedule. commands.py already
ran the Windows dialogs on a worker thread for exactly that reason, and that
is unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import portal
from dbusio import BUS  # noqa: F401  (re-exported for the fallback check)


def ask_save_path(default_name: str, initial_dir: "Path | None" = None,
                  fallback_dir: "Path | None" = None,
                  title: str = "Save screenshot",
                  filters: "list[tuple[str, list[str]]] | None" = None
                  ) -> Path | None:
    """A Save As dialog. The chosen path, or None when the user cancelled.

    The portal appends no extension of its own, so the filter list is
    advisory and the caller stays responsible for the suffix - same as the
    Windows version, where OFN_OVERWRITEPROMPT did the asking and
    lpstrDefExt did the appending.

    `fallback_dir` is where the file goes when there is no chooser at all -
    no session bus, no portal. Not when the user cancels: cancelling means
    they did not want the file, and writing it anyway would be the program
    overruling them.
    """
    folder = str(initial_dir) if initial_dir else ""
    if filters is None:
        filters = [("JPEG image", ["*.jpg", "*.jpeg"]),
                   ("PNG image", ["*.png"])]
    if not portal.BUS.connect():
        if fallback_dir is None:
            return None
        fallback_dir = Path(fallback_dir)
        fallback_dir.mkdir(parents=True, exist_ok=True)
        print(f"[dialogs] no file chooser on this session - saving to "
              f"{fallback_dir}", file=sys.stderr)
        return fallback_dir / default_name
    try:
        chosen = portal.save_file(title, default_name, folder, filters)
    except Exception as exc:
        print(f"[dialogs] the file chooser failed: {exc}", file=sys.stderr)
        return None
    return Path(chosen) if chosen else None


def pick_directory(title: str = "Choose a folder",
                   initial_dir: "Path | None" = None) -> Path | None:
    """A folder picker. The chosen directory, or None when cancelled."""
    folder = str(initial_dir) if initial_dir else ""
    try:
        chosen = portal.pick_folder(title, folder)
    except Exception as exc:
        print(f"[dialogs] the folder chooser failed: {exc}", file=sys.stderr)
        return None
    if not chosen:
        return None
    path = Path(chosen)
    return path if path.is_dir() else None


def save_jpeg(path: Path, rgba) -> bool:
    """Write an RGBA frame as a JPEG. False on any failure.

    Unchanged from the Windows build except for what does the writing:
    there it was the WIC encoder through COM, here it is Pillow, which the
    program already carries for the tray icon. Quality 95 and 4:4:4
    chroma - a screenshot of text at 4:2:0 is a screenshot of coloured
    fringes.
    """
    try:
        from PIL import Image

        array = rgba[:, :, :3] if rgba.shape[2] == 4 else rgba
        Image.fromarray(array, "RGB").save(
            path, "JPEG", quality=95, subsampling=0, optimize=True)
        return True
    except Exception as exc:
        print(f"[dialogs] could not write {path}: {exc}", file=sys.stderr)
        return False
