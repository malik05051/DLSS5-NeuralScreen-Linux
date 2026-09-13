"""The layer-shell bindings, generated on first use.

pywayland ships bindings for every protocol in wayland-protocols, and
wlr-layer-shell is not one of them: it lives in wlr-protocols, outside the
freedesktop tree, which is also why GNOME does not implement it. So the XML
travels with the program (native/protocols/) and the bindings are generated
from it the first time they are needed, into the user's cache.

Generating at run time rather than at build time is deliberate: the scanner
emits code against the installed pywayland's internals, and a package built
on one version and run against another is exactly the kind of breakage that
only shows up on somebody else's machine. The cache is keyed on the
pywayland version so an upgrade regenerates instead of loading stale code.

The generated module imports its dependencies relatively (`from .wayland
import WlOutput`). Those imports are rewritten to pywayland's own copies:
two independently generated WlSurface classes are two different interfaces
as far as the registry is concerned, and binding one where the other is
expected fails in a way that reads like a compositor bug.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path

from paths import BASE_DIR


XML = BASE_DIR / "native" / "protocols" / "wlr-layer-shell-unstable-v1.xml"

_lock = threading.Lock()
_cached = None          # the module, once imported
_failure = ""           # why it could not be, so we say it once


def _cache_dir() -> Path:
    raw = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(raw) if raw and Path(raw).is_absolute() else Path.home() / ".cache"
    try:
        import pywayland
        version = getattr(pywayland, "__version__", "0")
    except Exception:
        version = "0"
    return base / "neuralscreen" / f"wlproto-{version}"


def _generate(target: Path) -> bool:
    """Run the pywayland scanner into `target`. False when it cannot."""
    if not XML.is_file():
        global _failure
        _failure = f"{XML} is missing from the installation"
        return False
    import logging
    from pywayland.scanner import Protocol

    scratch = target.with_name(target.name + ".part")
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    # The scanner is chatty at INFO and this is a library call, not a build.
    level = logging.getLogger("pywayland").level
    logging.getLogger("pywayland").setLevel(logging.WARNING)
    try:
        protocol = Protocol.parse_file(str(XML))
        # The scanner writes `from .<protocol> import <Class>` for every
        # foreign interface, and decides the module from this map. The
        # foreign ones are wayland's own and xdg-shell's, both of which
        # pywayland already ships - so they are named here and the relative
        # imports are pointed at pywayland's copies below.
        imports = {"wl_output": "wayland", "wl_surface": "wayland",
                   "xdg_popup": "xdg_shell"}
        imports.update({iface.name: protocol.name
                        for iface in protocol.interface})
        protocol.output(str(scratch), imports)
    except Exception as exc:
        _fail(f"the scanner could not read {XML.name}: {exc}")
        shutil.rmtree(scratch, ignore_errors=True)
        return False
    finally:
        logging.getLogger("pywayland").setLevel(level)

    package = scratch / "nsproto"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    moved = False
    for src in list(scratch.rglob("wlr_layer_shell_unstable_v1*.py")):
        if src.parent == package:
            moved = True
            continue
        shutil.move(str(src), str(package / src.name))
        moved = True
    if not moved:
        _fail("the scanner produced no layer-shell module")
        shutil.rmtree(scratch, ignore_errors=True)
        return False
    for module in package.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        text = text.replace("from .wayland import",
                            "from pywayland.protocol.wayland import")
        text = text.replace("from .xdg_shell import",
                            "from pywayland.protocol.xdg_shell import")
        module.write_text(text, encoding="utf-8")
    shutil.rmtree(target, ignore_errors=True)
    scratch.rename(target)
    return True


def _fail(message: str) -> None:
    global _failure
    _failure = message
    print(f"[wayland] {message}", file=sys.stderr)


def layer_shell():
    """(ZwlrLayerShellV1, ZwlrLayerSurfaceV1), or None when unavailable.

    None is not a failure the caller has to shout about: it means this
    desktop gets the xdg-shell fallback, which is a worse overlay but a
    working program.
    """
    global _cached
    with _lock:
        if _cached is not None:
            return _cached
        if _failure:
            return None
        target = _cache_dir()
        package = target / "nsproto" / "wlr_layer_shell_unstable_v1.py"
        if not package.is_file() and not _generate(target):
            return None
        if str(target) not in sys.path:
            sys.path.insert(0, str(target))
        try:
            from nsproto.wlr_layer_shell_unstable_v1 import (  # type: ignore
                ZwlrLayerShellV1, ZwlrLayerSurfaceV1)
        except Exception as exc:
            _fail(f"the generated layer-shell bindings do not import: {exc}")
            return None
        _cached = (ZwlrLayerShellV1, ZwlrLayerSurfaceV1)
        return _cached


def reason() -> str:
    """Why there is no layer shell, for the log. "" when there is one."""
    return _failure
