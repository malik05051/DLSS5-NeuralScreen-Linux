"""Build the NeuralScreen release archive: the git files plus the artefacts.

The archive carries a VERSION.txt manifest (git commit, runtime SHA-256,
kernel architectures) so a user can tell exactly which build they have. The
build refuses to package a runtime whose kernels do not match the expected
set - the v1.5.0 mistake, a release that claimed "everything" and carried a
Blackwell-only runtime.

Two things changed with the move off Windows, and one of them shrank this
file by two thirds.

  * **A tarball, not a zip**, because the mode bits matter. `neuralscreen.sh`
    and the worker have to come out executable, and a zip does not carry
    that. xz rather than gzip: the archive is dominated by one 165 MB
    runtime and the extra compression is worth the seconds.

  * **No bundled interpreter.** The Windows archive shipped an embedded
    Python and most of this script was pruning it - a web frontend, a
    dataframe library and a bundler that had been dragged in for other work,
    130 MB of packages the program never touches. A Linux machine has
    Python, and neuralscreen.sh names the handful of modules that have to be
    installed and says how. So the archive is the program: sources, the
    built worker, the fonts, the runtime.

The archive is still exactly what the program needs to run and nothing else
(user rule 2026-09-08): no tests, no build scripts, no screenshots.
"""
import collections
import hashlib
import io
import os
import stat
import struct
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

# The script must work from any directory: every path is relative to git.
BASE = Path(__file__).resolve().parent
os.chdir(BASE)

VERSION = "1.8.2"
# What the bundled runtime is expected to run on. RTX 20 is listed as
# unsupported for a different reason than it was on Windows: the runtime
# carries sm_75 kernels either way, but the architecture spoof that got a
# pre-Blackwell card past NGX's own check was a Windows forwarder DLL and
# has no counterpart here. See TECHNICAL.md, "What did not come across".
TARGET_ARCHS = ("RTX 50 (sm_120). The sm_75/86/89 kernels are in the runtime, "
                "but NGX refuses the feature on pre-Blackwell cards and this "
                "build has no spoof")

#: Everything git knows about, plus the artefacts git deliberately does not.
files = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
extra = [
    "README.ru.md",
    # Built by native/linux/build-host.sh, gitignored on purpose.
    "native/linux/neuralscreen-host",
    # The Proton side of the neural renderer: our exe, built by
    # native/proton/build.sh, and NVIDIA's own runtime beside it - 165 MB,
    # over GitHub's file limit, so it is never in git and always in the
    # archive.
    "native/proton/nvngx.dll_nr.exe",
    "native/proton/nvngx_dlssnr.dll",
]

#: Repository files that are not the program. The tests and the builders are
#: for whoever works on it; the screenshots are for the README on GitHub.
DEV_ONLY = {
    "build_release.py",
    "verify_github.py",
    ".gitattributes",
    ".gitignore",
}

#: Files under native/ the program LOADS at run time, as opposed to the
#: sources it was built from. Named one by one rather than matched by
#: extension: the rule is "these files", and a .png dropped into native/
#: tomorrow is still developer baggage.
#:
#: The .xml earns its place the hard way - wlproto.py generates the
#: layer-shell bindings from it at first run, and without it the overlay
#: silently falls back to an xdg-shell window that cannot be click-through.
RUNTIME_ASSETS = (
    "native/neuralscreen.png",
    "native/linux/neuralscreen-host",
    "native/linux/libns-archspoof.so",
    "native/proton/nvngx.dll_nr.exe",
    "native/proton/nvngx_dlssnr.dll",
    "native/protocols/wlr-layer-shell-unstable-v1.xml",
)


def _skip(path: str) -> bool:
    norm = path.replace("\\", "/")
    if norm in DEV_ONLY or norm.startswith("tests/") or norm.startswith("test_"):
        return True
    # docs/ holds the README screenshots - repository assets, not program
    # code. HDR.md is linked from the README and lives there too; it is
    # small and it is what a user reads when the picture goes wrong, so it
    # is the one thing kept out of that folder.
    if norm.startswith("docs/") and norm != "docs/HDR.md":
        return True
    # native/ is a source tree: the C++ the worker was built from, the NGX
    # headers, the build script. The archive carries the built worker, and
    # it could not be rebuilt from the archive anyway - the repository has
    # all of it.
    if norm.startswith("native/") and norm not in RUNTIME_ASSETS:
        return True
    if norm.endswith(".pyc") or "/__pycache__/" in norm:
        return True
    return False


seen = set()
uniq = []
for f in files + extra:
    norm = f.replace("\\", "/")
    if norm in seen or _skip(norm):
        continue
    seen.add(norm)
    uniq.append(norm)

out = f"neuralscreen-v{VERSION}-full.tar.xz"
#: Everything lands under one directory, so unpacking in a home folder does
#: not scatter twenty files into it.
TOP = f"neuralscreen-v{VERSION}"

# The runtime kernel check: the v1.5.0 archive shipped a Blackwell-only
# runtime as if it were universal. The kernel names live inside CUDA fatbin
# records (compressed cubins - a plain string search finds nothing). Port of
# the proven parser from DLSS5-Autopilot (core/gpu.py, MIT).
#
# It works unchanged on an ELF: the records are CUDA's own container format
# and know nothing about PE or ELF around them.
_FATBIN_MAGIC = struct.pack("<I", 0xBA55ED50)
SM_NAMES = {75: "sm_75", 86: "sm_86", 89: "sm_89", 120: "sm_120"}
KNOWN_SM = set(SM_NAMES) | {50, 52, 53, 60, 61, 62, 70, 72, 80, 90, 100, 101, 110}


def runtime_architectures(path: str) -> set[int]:
    """Supported sm versions, from the CUDA fatbin records inside the file."""
    try:
        d = Path(path).read_bytes()
    except OSError:
        return set()
    found: collections.Counter[int] = collections.Counter()
    off = 0
    while True:
        i = d.find(_FATBIN_MAGIC, off)
        if i < 0:
            break
        off = i + 4
        try:
            hsize = struct.unpack_from("<H", d, i + 6)[0]
            fatsize = struct.unpack_from("<Q", d, i + 8)[0]
            if hsize < 16 or not (0 < fatsize <= len(d)):
                continue
            p, end = i + hsize, i + hsize + fatsize
            while p < end - 32:
                ehdr = struct.unpack_from("<I", d, p + 4)[0]
                payload = struct.unpack_from("<Q", d, p + 8)[0]
                if ehdr < 24 or ehdr > 4096 or not (0 < payload <= len(d)):
                    break
                for so in (24, 28, 20):
                    if p + so + 4 > len(d):
                        continue
                    sm = struct.unpack_from("<I", d, p + so)[0]
                    if sm in KNOWN_SM:
                        found[sm] += 1
                        break
                p += ehdr + payload
        except Exception:
            continue
    return set(found)


snippet_path = Path("native/proton/nvngx_dlssnr.dll")
nr_exe = Path("native/proton/nvngx.dll_nr.exe")
if not nr_exe.is_file():
    raise SystemExit(f"{nr_exe} is not built - run native/proton/build.sh")
if not snippet_path.is_file():
    raise SystemExit(
        f"{snippet_path} is missing - it is NVIDIA's own runtime and is "
        "never in git. See README.md, 'What you need'.")
snippet_data = snippet_path.read_bytes()
snippet_sha = hashlib.sha256(snippet_data).hexdigest()
print(f"runtime: {snippet_path.name} {len(snippet_data)} bytes, "
      f"sha256 {snippet_sha[:16]}...")
archs = runtime_architectures(str(snippet_path))
print("  kernels found: "
      + (", ".join(sorted(SM_NAMES.get(a, f"sm_{a}") for a in archs)) or "NONE"))
for want in (75, 86, 89, 120):
    if want not in archs:
        raise SystemExit(
            f"RUNTIME MISMATCH: sm_{want} not found in {snippet_path.name} - "
            "the archive would not run on the claimed cards. Refusing to "
            "build.")
    print(f"  kernel sm_{want}: ok")

worker = Path("native/linux/neuralscreen-host")
if not worker.is_file():
    raise SystemExit(f"{worker} is not built - run native/linux/build-host.sh")
if b"WGCW" not in worker.read_bytes():
    raise SystemExit(f"{worker} has no WGCW (window mode) - an old build?")

# The manifest: built-from commit, runtime identity, target architectures.
# The user can verify which build they have without asking anyone.
commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
version_txt = (
    f"NeuralScreen {VERSION}\n"
    f"commit: {commit}\n"
    f"runtime: nvngx_dlssnr.dll sha256 {snippet_sha}\n"
    f"kernel archs: "
    f"{', '.join(sorted(SM_NAMES.get(a, f'sm_{a}') for a in archs))}\n"
    f"targets: {TARGET_ARCHS}\n"
    f"built: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
)

#: What has to come out executable. A tarball carries the mode bits, which
#: is the whole reason this is not a zip: a launcher that unpacks without
#: the execute bit is a launcher that does not launch.
EXECUTABLE = {"neuralscreen.sh", "native/linux/neuralscreen-host"}


def _entry(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(f"{TOP}/{name}")
    info.mode = 0o755 if name in EXECUTABLE else 0o644
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    # Owned by nobody in particular: an archive that unpacks as the
    # maintainer's uid is an archive that surprises whoever unpacks it.
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


written = 0
with tarfile.open(out, "w:xz", preset=6) as tar:
    manifest = version_txt.encode("utf-8")
    info = _entry("VERSION.txt")
    info.size = len(manifest)
    tar.addfile(info, io.BytesIO(manifest))
    written += 1

    for f in uniq:
        if not os.path.isfile(f):
            print("MISSING:", f)
            continue
        # config.json comes ONLY from git HEAD, never from disk: the working
        # copy holds the developer's personal menu_offset/menu_scale/theme
        # and those must not ship. Comparing against the worktree is NOT
        # enough - a staged personal config (git add) would make the diff
        # clean and the personal values would leak into the archive
        # (audit #2, R1).
        if f == "config.json":
            try:
                data = subprocess.check_output(
                    ["git", "show", "HEAD:config.json"])
            except subprocess.CalledProcessError:
                data = Path(f).read_bytes()   # never committed - take it from disk
            info = _entry(f)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            written += 1
            continue
        info = _entry(f)
        info.size = os.path.getsize(f)
        with open(f, "rb") as handle:
            tar.addfile(info, handle)
        written += 1

print("entries:", written)
print("size:", os.path.getsize(out))
print(f"unpacks into {TOP}/ - run ./{TOP}/neuralscreen.sh")
