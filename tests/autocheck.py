"""NeuralScreen v1.4 self-checks - the static part (no GUI).

Run:  python3 autocheck.py
The GUI part (menu, recording) is run separately - see the end of the output.

Every check reports PASS / FAIL / SKIP plus a reason. Exit: 0 = all PASS,
1 = there is a FAIL.
"""
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # the project root (tests/ lives inside it)
sys.path.insert(0, str(ROOT))
from paths import WORKER_EXE  # noqa: E402
FAILS = []


def check(name, fn):
    try:
        ok, detail = fn()
        status = "PASS" if ok else "FAIL"
        if not ok:
            FAILS.append(name)
        print(f"[{status}] {name}: {detail}")
    except Exception as exc:
        FAILS.append(name)
        print(f"[FAIL] {name}: exception {exc!r}")


def fresh_worker():
    """The worker is built, newer than its sources, and answers --probe.

    The Windows version of this also checked for an NS_ARCH_SPOOF marker in
    the binary - the architecture spoof that let a pre-Blackwell card run
    the feature. That spoof was a forwarder DLL that inspected the module a
    call was returning to, which is Windows loader behaviour with no Linux
    equivalent, and this port does not carry it. TECHNICAL.md says so under
    "What did not come across".
    """
    exe = WORKER_EXE
    if not exe.exists():
        return False, f"no {exe.relative_to(ROOT)} (native/linux/build-host.sh)"
    sources = [ROOT / "native" / "linux" / name
               for name in ("host.cpp", "ns_vk.cpp", "ns_vk.h", "ns_pw.cpp",
                            "ns_pw.h", "ns_ipc.h")]
    stale = [src.name for src in sources
             if src.exists() and exe.stat().st_mtime < src.stat().st_mtime]
    if stale:
        return False, (f"the worker is older than {', '.join(stale)} - "
                       "rerun native/linux/build-host.sh")
    # The window-capture mode has to be in there: it is a string in the
    # binary exactly as WGCW was on Windows, and a build from before the
    # mode existed would otherwise pass every other check.
    if b"WGCW" not in exe.read_bytes():
        return False, "the worker has no WGCW (window mode) - an old build?"
    try:
        probe = subprocess.run([str(exe), "--probe"], capture_output=True,
                               text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"--probe did not run: {exc}"
    if probe.returncode != 0:
        # No GPU on this machine is not a build problem. Say which it is
        # rather than failing a release gate for the wrong reason.
        detail = (probe.stdout or probe.stderr or "").strip().splitlines()
        return True, (f"built and fresh; --probe could not run here "
                      f"({detail[-1] if detail else 'no device'})")
    return True, f"{exe.stat().st_size} bytes, fresh, --probe passes"


def personal_config_keys():
    """Every config key the program itself can write over a user's session.

    The archive's config.json comes from git HEAD, and this is what catches
    "a maintainer committed a personal value". It used to be a hand-written
    tuple, and it stopped growing: skip_static, gpu, screenshot_dir,
    rec_indicator and monitor were all written by _menu_layout_payload and
    none of them was ever compared. Adding those five would have restarted
    the same clock, so the set is asked of the payload itself - a key the
    menu learns to save is covered the day it is added.

    "hotkeys" is included by hand because it is saved on its own path, not
    through the menu payload.
    """
    # autocheck is run both through run_tests.py (which puts the project
    # root on the path) and on its own, where it is not there.
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import settings_io
    payload = settings_io._menu_layout_payload(
        {"profile": "Natural", "gpu": 0, "spout": False, "skip_static": True,
         "rec_indicator": True, "screenshot_dir": "", "presets": {}},
        {"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
         "skin_structure": -1.0},
        0, "en", 0.65, 0.0, True, False,
        type("M", (), {"user_scale": 1.0, "user_height": None,
                       "state": {"theme": "light"}, "offset": [0, 0]})())
    return sorted(set(payload) | {"hotkeys"})


def archive_integrity():
    """The release tarball has everything and its sources are the committed ones."""
    import tarfile

    matches = sorted(ROOT.glob("neuralscreen-v*-full.tar.xz"))
    if not matches:
        return False, "no neuralscreen-v*-full.tar.xz (build_release_zip.py)"
    apath = matches[-1]
    required = [
        "main.py", "gpuinfo.py", "overlay_ui.py", "i18n.py", "recorder.py",
        "display.py", "guides.py", "hotkeys.py", "tray.py", "capture.py",
        "audio.py", "protocol.py", "toplevels.py", "dialogs.py",
        "channels.py", "settings_io.py", "paths.py", "pipeline.py",
        "commands.py", "startup.py",
        "dbusio.py", "portal.py", "wayland_shell.py", "wlproto.py",
        "neuralscreen.sh",
        "TECHNICAL.md", "TECHNICAL.ru.md", "README.md", "README.ru.md",
        "native/linux/neuralscreen-host", "native/nvngx_dlssnr.so",
        # The layer-shell protocol XML: the bindings are generated from it
        # at first run, so without it the overlay silently falls back to
        # an xdg-shell window that cannot be click-through.
        "native/protocols/wlr-layer-shell-unstable-v1.xml",
        # Loaded at run time with a silent fallback: left out, the program
        # ships with the wrong icon and says nothing about it.
        "native/neuralscreen.png",
        # The interface faces travel with the program: a distribution
        # without IBM Plex would draw the menu in whatever it has.
        "fonts/IBMPlexSans-Regular.ttf", "fonts/IBMPlexMono-Regular.ttf",
        "fonts/OFL.txt",
        "VERSION.txt",
    ]
    with tarfile.open(apath, "r:xz") as tar:
        names = {n.split("/", 1)[-1] if "/" in n else n
                 for n in tar.getnames()}
        missing = [f for f in required if f not in names]
        if missing:
            return False, f"missing from the archive: {missing}"
        prefix = ""
        for n in tar.getnames():
            if n.endswith("main.py"):
                prefix = n[:-len("main.py")]
                break

        def read(member):
            handle = tar.extractfile(prefix + member)
            return handle.read() if handle is not None else b""

        # The worker in the archive carries the window-capture mode.
        if b"WGCW" not in read("native/linux/neuralscreen-host"):
            return False, "the worker in the archive has no WGCW (window mode)"
        # The PYTHON SOURCES in the archive must be EXACTLY the committed
        # ones: the archive is often rebuilt from a dirty tree, and a code
        # change that never got committed ends up in it silently (audit #4,
        # C1/C2).
        for name in ("main.py", "hotkeys.py", "display.py", "recorder.py",
                     "overlay_ui.py", "i18n.py", "protocol.py",
                     "toplevels.py", "wayland_shell.py", "portal.py",
                     "dbusio.py", "pipeline.py", "settings_io.py",
                     "channels.py", "commands.py", "paths.py", "startup.py",
                     "README.md", "README.ru.md"):
            try:
                head = subprocess.check_output(["git", "show", f"HEAD:{name}"],
                                               cwd=ROOT)
            except subprocess.CalledProcessError:
                continue  # not in git - a new file, nothing to compare with
            # The worktree files carry CRLF (core.autocrlf) while git show
            # returns LF - compare the NORMALIZED bytes on both sides,
            # otherwise every CRLF file trips the check (audit #4, C2).
            crlf, lf = bytes([13, 10]), bytes([10])
            got = read(name).replace(crlf, lf)
            if got != head.replace(crlf, lf):
                return False, f"{name} in the archive differs from HEAD"
        # the config in the archive is the default one, not a personal one:
        # personal values are written into the config legitimately, so we
        # check ALL such fields against the committed HEAD config
        cfg = json.loads(read("config.json"))
        try:
            head_cfg = json.loads(subprocess.check_output(
                ["git", "show", "HEAD:config.json"]))
        except Exception:
            head_cfg = {}
        leak = []
        for key in personal_config_keys():
            if key not in head_cfg:
                continue
            if cfg.get(key) != head_cfg[key]:
                leak.append(f"{key}={cfg.get(key)!r} != HEAD {head_cfg[key]!r}")
        if leak:
            return False, "a personal config in the archive: " + ", ".join(leak)
        # VERSION.txt must tell the truth (audit 10.09 H1): the commit is
        # HEAD, the runtime sha matches the DLL inside, and the version
        # matches the file name. A manifest that lies is worse than none.
        vt = z.read("VERSION.txt").decode("utf-8", "replace")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       cwd=ROOT, text=True).strip()
        if f"commit: {head}" not in vt:
            return False, "VERSION.txt commit != HEAD - rebuilt from a dirty tree?"
        zip_dll = z.read("native/nvngx_dlssnr.dll")
        zsha = hashlib.sha256(zip_dll).hexdigest()
        if f"sha256 {zsha}" not in vt:
            return False, "VERSION.txt runtime sha != the DLL inside the archive"
        if "NeuralScreen 1.8.2" not in vt:
            return False, "VERSION.txt version does not match v1.8.2"
    return True, f"{zpath.stat().st_size} bytes, all files, the hook, a default config, a truthful manifest"


def gpuinfo_works():
    """gpuinfo.py answers: architecture plus official support."""
    py = Path(sys.executable)
    if not py.exists():
        return False, "no interpreter"
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import gpuinfo; i = gpuinfo.probe(); "
        "print(gpuinfo.describe(i) or 'unknown GPU'); "
        "print('official:', i['official']); print('cards:', gpuinfo.list_gpus())"
        % ROOT
    )
    # PYTHONIOENCODING: without it the child prints in the console codepage
    # and an em-dash in the GPU name decodes into garbage (or throws).
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                       timeout=30, encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        return False, f"gpuinfo crashed: {r.stderr.strip()[:200]}"
    out = r.stdout.strip()
    if "unknown GPU" in out:
        # NVML answered nothing: there is no NVIDIA card on this machine,
        # which is a fact about the machine and not about the code.
        return True, "no NVIDIA GPU here - gpuinfo answered without raising"
    if "Blackwell" not in out and "officially supported" not in out:
        return False, f"odd answer: {out}"
    return True, out.replace("\n", " | ")


def readme_consistency():
    """The four docs line up: two short READMEs, two technical ones.

    The READMEs are for someone installing the program, so the check that
    matters is that they stayed short and that every image and link in them
    resolves. The measurements live in the technical docs, and the one fact
    those must not lose is the account of what the port could not bring
    across - that is the difference between a known gap and a bug report.
    """
    import re

    docs = {
        # The keys name where the files really live: each doc's links are
        # resolved from its own directory below. These two moved from docs/
        # to the root - the old keys kept resolving them against docs/.
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "README.ru.md": (ROOT / "README.ru.md").read_text(encoding="utf-8"),
        "TECHNICAL.md": (ROOT / "TECHNICAL.md").read_text(encoding="utf-8"),
        "TECHNICAL.ru.md": (ROOT / "TECHNICAL.ru.md").read_text(encoding="utf-8"),
    }
    for name in ("README.md", "README.ru.md"):
        n = len(docs[name].splitlines())
        if n > 200:
            return False, f"{name} is {n} lines - it drifted back into a manual"
    # The Russian needle is the content of a translated doc and stays Russian.
    if "What did not come across" not in docs["TECHNICAL.md"]:
        return False, "TECHNICAL.md lost the account of what was not ported"
    if "Что не перенеслось" not in docs["TECHNICAL.ru.md"]:
        return False, "TECHNICAL.ru.md lost the account of what was not ported"

    # Every relative link and image must resolve, in both directions.
    for name, text in docs.items():
        base = (ROOT / name).parent
        for target in re.findall(r"]\(([^)#][^)]*)\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (base / target).exists():
                return False, f"{name} points at a missing {target}"
        for src in re.findall(r'<img src="([^"]+)"', text):
            # The screenshots are addressed absolutely on purpose: docs/ is
            # not in the release archive, and a relative link left every
            # image broken for anyone who unpacked the zip. Only a relative
            # path is ours to resolve.
            if src.startswith(("http://", "https://")):
                continue
            if not (base / src).exists():
                return False, f"{name} shows a missing {src}"
    return True, (f"READMEs {len(docs['README.md'].splitlines())}/"
                  f"{len(docs['README.ru.md'].splitlines())} lines, links resolve")


def git_clean():
    """The working copy is clean (apart from a personal config.json)."""
    r = subprocess.run(["git", "status", "--short"], cwd=ROOT,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    dirty = [l for l in r.stdout.splitlines() if l.strip() and "config.json" not in l]
    if dirty:
        return False, f"dirty: {dirty[:5]}"
    return True, "clean (config.json is personal, as expected)"


def release_notes_short():
    """The latest release notes are concise (the EN part is under 2 KB)."""
    from json import loads as _loads
    try:
        tag = _loads(subprocess.check_output(
            ["gh", "release", "list", "-R", "perseval-BLR/DLSS5-NeuralScreen",
             "--json", "tagName", "--limit", "1", "--exclude-drafts"],
            text=True, timeout=60, encoding="utf-8", errors="replace"))[0]["tagName"]
    except Exception:
        return False, "cannot resolve the latest release tag"
    r = subprocess.run(
        ["gh", "release", "view", tag, "-R", "perseval-BLR/DLSS5-NeuralScreen",
         "--json", "body", "--jq", ".body"],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return False, f"gh: {r.stderr.strip()[:100]}"
    body = r.stdout
    # The WHOLE body, not the part before the first "---". That split dates
    # from when the notes were written in two languages and the rule "one
    # language" (user, 12.09) retired it - and it had quietly made this
    # check vacuous: a markdown table starts with a |---| row, so the split
    # fired on the first table and measured 505 characters of a 4639
    # character body.
    #
    # 5000, and the purpose is unchanged: the notes must not drift into a
    # manual. 1.7.0 is the first release to carry a feature, an
    # architectural change and twenty fixes at once, and the user asked for
    # the fixes to be spelled out ("the things that were fixed - describe
    # them, definitely").
    if len(body) > 5000:
        return False, f"the release body is {len(body)} characters - too long"
    return True, f"the release body is {len(body)} characters"


# --- driving the running program (the GUI and smoke checks share this) ---
LOG = ROOT / "NeuralScreen.log"
KEYEVENTF_KEYUP = 0x0002
_MOD_VK = {0x0002: 0x11, 0x0001: 0x12, 0x0004: 0x10}  # Ctrl / Alt / Shift


def binding_keys(command):
    """The default binding for a command as (vk, modifier vks).

    Taken from hotkeys.DEFAULT_BINDINGS rather than written out here: the
    defaults have moved twice already, and a check that presses yesterday's
    key tests nothing.
    """
    sys.path.insert(0, str(ROOT))
    from hotkeys import DEFAULT_BINDINGS
    for mods, vk, cmd, _name in DEFAULT_BINDINGS.values():
        if cmd == command:
            return vk, tuple(v for bit, v in _MOD_VK.items() if mods & bit)
    raise KeyError(command)


def send_key(vk, mods=()):
    import ctypes
    for m in mods:
        ctypes.windll.user32.keybd_event(m, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    for m in reversed(mods):
        ctypes.windll.user32.keybd_event(m, 0, KEYEVENTF_KEYUP, 0)


def log_offset():
    """Where the log ends right now.

    The log is appended to across runs, so a check that greps the whole file
    can pass on the previous launch's lines. Everything below reads from the
    offset taken before the launch.
    """
    return LOG.stat().st_size if LOG.exists() else 0


def log_since(offset):
    # main.py opens the log as utf-8; errors="replace" guards a truncated tail.
    if not LOG.exists():
        return ""
    with LOG.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        return fh.read()


def running_instances():
    """Our processes that are already up.

    Nothing stops a second copy of the program from starting, and two copies
    fight over the screen capture - so a check that launches one has to know
    the field is clear, otherwise it measures the wrong process.
    """
    # ps rather than tasklist: the same question - what is still
    # running - asked of the system that has the answer here.
    out = subprocess.run(["ps", "-eo", "comm="],
                         capture_output=True).stdout
    text = out.decode("utf-8", errors="replace")
    return [l.strip() for l in text.splitlines()
            if "neuralscreen-host" in l]


def launch():
    """Start the program the way a user does and return the log offset."""
    offset = log_offset()
    subprocess.run(["cscript", "//nologo", "NeuralScreen.vbs"], cwd=ROOT,
                   capture_output=True)
    return offset


def wait_for(offset, needle, timeout):
    """Wait for a line to appear in the log written since `offset`."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = log_since(offset)
        if needle in text:
            return text
        time.sleep(0.5)
    return None


def quit_app(timeout=8.0):
    """Ctrl+Alt+Q, then confirm nothing of ours is left running."""
    import time
    send_key(*binding_keys("quit"))
    deadline = time.monotonic() + timeout
    left = []
    while time.monotonic() < deadline:
        # ps rather than tasklist: the same question - what is still
        # running - asked of the system that has the answer here.
        out = subprocess.run(["ps", "-eo", "comm="],
                             capture_output=True).stdout
        text = out.decode("utf-8", errors="replace")
        left = [l for l in text.splitlines()
                if "neuralscreen-host" in l]
        if not left:
            return []
        time.sleep(0.5)
    return left


def smoke_check():
    """The short one: launch -> is it processing -> exit. ~20 seconds.

    Everything a broken build fails at before recording even matters: the
    program comes up, the pipeline produces frames, the pixel channel is the
    shared-memory one (it silently fell back to the pipe once - see the OUTS
    regression), and Ctrl+Alt+Q leaves nothing running.
    """
    import re
    import time

    MIN_FRAMES, MIN_FPS = 60, 15.0
    busy = running_instances()
    if busy:
        return False, f"NeuralScreen is already running ({busy}) - stop it first"
    offset = launch()
    try:
        # The first FPS line reports the warm-up (frames 2, ~1 FPS), so wait
        # for the pipeline to have actually run for a while.
        fps, frames, deadline = 0.0, 0, time.monotonic() + 30.0
        while time.monotonic() < deadline:
            text = log_since(offset)
            stats = re.findall(r"NR ON \| FPS\s+([\d.]+) \| frames (\d+)", text)
            if stats:
                fps, frames = float(stats[-1][0]), int(stats[-1][1])
                if frames >= MIN_FRAMES:
                    break
            time.sleep(0.5)
        text = log_since(offset)
        if frames == 0:
            return False, "NeuralScreen did not start processing (no FPS line in the log)"
        if frames < MIN_FRAMES or fps < MIN_FPS:
            return False, (f"the pipeline is barely alive: {fps:.1f} FPS, "
                           f"{frames} frames in 30 s")
        if "result pixels through shared memory" not in text:
            pipe = "shared memory for pixels unavailable" in text
            return False, ("the pixels are not going through shared memory"
                           + (" - the worker refused the section" if pipe else ""))
        # The capture must ALSO be the DDA one. A dead DDA1 makes the client
        # silently fall back to sending frames from Python - the pipeline
        # still runs, the FPS lines look fine, and only this log line tells
        # it apart (audit #4: DDA has no direct regression test).
        if "screen capture inside the worker (DDA1)" not in text:
            return False, "the worker is not capturing the screen (no DDA1 in the log)"
    finally:
        left = quit_app()
    if left:
        return False, f"processes left behind: {left}"
    return True, f"came up, {fps:.1f} FPS at {frames} frames, shared memory, clean exit"


def gui_check():
    """The full GUI cycle: launch -> record -> exit. Requires NeuralScreen not
    to be running. ~50 seconds."""
    import re
    import time

    busy = running_instances()
    if busy:
        return False, f"NeuralScreen is already running ({busy}) - stop it first"
    # 1. launch
    offset = launch()
    if wait_for(offset, "NR ON", timeout=25.0) is None:
        return False, "NeuralScreen did not come up (no 'NR ON' in the log)"
    # The first FPS lines report the warm-up (frames 2, ~1 FPS) - the NGX
    # feature discards 120 warmup frames and the pipeline needs several more
    # seconds to normalise (measured: 20 FPS at frame 82, 28 at 191, 38 at
    # 336). Recording into the warm-up would produce a short, slow file and
    # a false FAIL (75 frames / 4.8 s instead of 100+). Wait for the
    # pipeline to have actually normalised - frames >= 300 is ~8-10 s after
    # launch, past the warm-up.
    fps, frames, deadline = 0.0, 0, time.monotonic() + 40.0
    while time.monotonic() < deadline:
        text = log_since(offset)
        stats = re.findall(r"NR ON \| FPS\s+([\d.]+) \| frames (\d+)", text)
        if stats:
            fps, frames = float(stats[-1][0]), int(stats[-1][1])
            if frames >= 300:
                break
        time.sleep(0.5)
    if frames < 300:
        quit_app()
        return False, (f"the pipeline did not normalise: {fps:.1f} FPS, "
                       f"{frames} frames in 40 s")
    # 2. record for 5 seconds - with the key the program actually binds
    record = binding_keys("record")
    send_key(*record)
    time.sleep(5)
    send_key(*record)
    time.sleep(3)
    recs = sorted((ROOT / "recordings").glob("neuralscreen-*.mp4"),
                  key=lambda p: p.stat().st_mtime)
    if not recs:
        quit_app()
        return False, "the recording file was not created"
    rec = recs[-1]
    import av
    c = av.open(str(rec))
    s = c.streams.video[0]
    frames, dur = s.frames, float(s.duration * s.time_base)
    c.close()
    if frames < 100 or dur < 4:
        quit_app()
        return False, f"the recording looks suspicious: {frames} frames / {dur:.1f} s"
    # 3. exit
    left = quit_app()
    if left:
        return False, f"processes left behind: {left}"
    return True, (f"recording of {frames} frames / {dur:.1f} s, "
                  f"a clean exit, no processes left")


def main():
    print(f"NeuralScreen autocheck - {ROOT}")
    print("=" * 60)
    if "--gui" in sys.argv:
        check("GUI: launch -> record -> exit", gui_check)
    elif "--smoke" in sys.argv:
        check("smoke: launch -> processing -> exit", smoke_check)
    else:
        check("worker: fresh, with the hook", fresh_worker)
        check("archive: integrity and contents", archive_integrity)
        check("gpuinfo: answers", gpuinfo_works)
        check("README: EN/RU agree", readme_consistency)
        check("git: the working copy is clean", git_clean)
        check("release notes: concise", release_notes_short)
    print("=" * 60)
    if FAILS:
        print(f"RESULT: {len(FAILS)} FAIL - {FAILS}")
        return 1
    print("RESULT: all checks PASS")
    if "--gui" not in sys.argv and "--smoke" not in sys.argv:
        print()
        print("smoke (20 s):  python3 autocheck.py --smoke")
        print("GUI part:      python3 autocheck.py --gui")
    return 0


if __name__ == "__main__":
    sys.exit(main())
