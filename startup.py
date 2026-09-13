"""Getting the program ready: from a config file to a running pipeline.

A straight line that runs once, kept away from the loop that runs sixty times
a second. Two halves, in the order they happen:

  configure()  decides what the program is going to do - the config and the
               NR parameters, which monitor, and at what resolution. The
               resolution comes from the REAL monitor rather than from the
               config: a display switched to 1440p while config.json still
               says 4K would leave the overlay, the recording and the worker
               window drifting away from the screen.
  bring_up()   makes it exist - the shared memory, the worker, the overlay,
               the tray icon, the taskbar button, the hotkeys, the guides,
               and every default on the state object the loop then reads.

Order is the whole content of this module. The worker is started before the
overlay because it is the slow part; the menu captions are set only after the
hotkeys are registered, because before that there are no bindings to name;
and the state defaults come last, so nothing the loop reads is missing when
the first frame arrives.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading

import numpy as np

from capture import ScreenCapture, resolve_output_idx
from display import Display
from gpuinfo import describe as gpu_describe, probe as gpu_probe
from guides import TemporalGuideGenerator
from hotkeys import (HotkeyController, build_bindings,
                     describe as describe_hotkeys, numlock_needed,
                     numlock_on)
from i18n import STRINGS as UI_STRINGS
from paths import BASE_DIR, log_path
from pipeline import start_worker
from protocol import SharedFrameBuffer, WorkerReader
from recorder import VideoRecorder
from settings_io import (APP_VERSION, _work_size, hotkey_labels, load_config,
                         load_presets, resolve_params)
from taskbar import TaskbarWindow
from tray import TrayController


# --- Log to a file instead of the console --------------------------------
# The release is launched through pythonw.exe (no console window): stdout and
# stderr are None there and any print would fail. We redirect them into
# NeuralScreen.log next to main.py - every print keeps working and the user
# reads the log as a file rather than a window. Startup errors (a missing DLL
# and the like) are additionally shown in a message box (see the bottom of
# this file).
LOG_PATH = log_path()


def _init_logging() -> None:
    """Redirect stdout/stderr into NeuralScreen.log (utf-8)."""
    try:
        log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = log_file
        sys.stderr = log_file
    except Exception:
        pass  # it did not work - the prints just vanish, we do not crash


def _apply_nr_dll(cfg: dict) -> None:
    """The swappable runtime: a configured nr_dll reaches the worker.

    The worker loads nvngx_dlssnr.so by name; NS_NR_DLL lets a different
    build be loaded without rebuilding the worker (the RHI
    dlss_manifest.json pattern). The path is put into the environment,
    which subprocess inherits. Without the flag the bundled runtime stays
    the default.
    """
    if cfg.get("nr_dll"):
        os.environ["NS_NR_DLL"] = str(cfg["nr_dll"])


def _apply_spout_env(cfg: dict) -> None:
    """The PipeWire-output flag reaches the worker through the environment.

    The worker reads NS_SPOUT once, at process start - there is no protocol
    message for the output node, so the config flag becomes the environment
    before the first worker is launched (and again on every restart, see
    pipeline.apply_spout). "0" and unset both mean off; the worker treats
    anything but "1" as disabled.

    The name is the Windows one, kept so an existing config and an existing
    launch script keep working; what it switches on is a PipeWire video
    source rather than a Spout2 shared texture. See pipeline.apply_spout.
    """
    os.environ["NS_SPOUT"] = "1" if cfg.get("spout") else "0"


def _apply_hdr_env(cfg: dict) -> None:
    """The HDR compatibility flag reaches the worker the same way.

    HdrEnabled() in the worker reads NS_HDR once, at process start, and
    everything downstream of it is decided then: which PipeWire format is
    accepted, the swapchain format, the colour space. So the switch goes
    through a worker restart (pipeline.apply_hdr), exactly like the
    PipeWire output. Off unless the config says otherwise - the mode is
    experimental.
    """
    os.environ["NS_HDR"] = "1" if cfg.get("hdr") else "0"


def _apply_monitor_env(capture) -> tuple[int, int]:
    """Publish the chosen monitor to the worker and return its origin.

    Three hand-offs for the same subject:

    * NS_OUTPUT - the connector name of the monitor being processed
      ("DP-1"), so the worker's own log names the same screen the menu
      does.
    * NS_PW_NODE and NS_PW_FD_SOURCE - the PipeWire node the portal
      granted, and the descriptor to read it on. These are what actually
      select the source: the worker does not go looking for a monitor, it
      reads the stream it was handed. pipeline.start_worker turns the
      second one into an inherited descriptor.
    * the origin - where the chosen monitor's corner sits in the
      compositor's logical space. The caller feeds it to
      Display.set_origin.

    A monitor whose name cannot be resolved keeps the defaults - the old
    behaviour - and says so in the log.
    """
    from capture import monitor_origin
    node = getattr(capture, "node_id", -1)
    fd = getattr(capture, "pipewire_fd", -1)
    if node is not None and node >= 0:
        os.environ["NS_PW_NODE"] = str(node)
    else:
        os.environ.pop("NS_PW_NODE", None)
    if fd is not None and fd >= 0:
        os.environ["NS_PW_FD_SOURCE"] = str(fd)
    else:
        os.environ.pop("NS_PW_FD_SOURCE", None)
    name = getattr(capture, "devicename", "") or ""
    if name:
        os.environ["NS_OUTPUT"] = name
        origin = monitor_origin(name)
        if origin is not None:
            os.environ["NS_WINDOW_POS"] = f"{origin[0]},{origin[1]}"
            return origin
    os.environ.pop("NS_OUTPUT", None)
    os.environ.pop("NS_WINDOW_POS", None)
    print(f"[main] monitor identity unknown ({name!r}) - "
          f"output 0 and the primary position stay", file=sys.stderr)
    return (0, 0)


def _apply_gpu_env(cfg: dict) -> None:
    """Which card the worker runs on, through the environment.

    NS_GPU is read once per worker process - the Vulkan physical device is
    chosen before the logical device exists - so the config flag becomes
    the environment before the first worker is launched, and again on every
    restart (see pipeline.apply_gpu). Unset means the worker's own default:
    the first NVIDIA device that can import the stream's buffers.
    """
    gpu = cfg.get("gpu")
    if gpu is None:
        os.environ.pop("NS_GPU", None)
    else:
        os.environ["NS_GPU"] = str(int(gpu))


def _log_environment(cfg: dict) -> None:
    """Print the environment header into the log: version, session, GPU.

    Users paste NeuralScreen.log into issues; the header answers the
    questions we would otherwise have to ask. On Windows those were the
    build number, the driver version out of the display-class registry key
    and whether HDR was on in the monitor data store - three registry
    walks. Here the same facts come from places that are meant to be read:
    the compositor names itself in the environment, NVML knows the driver,
    and the portal knows which backend is answering.

    Every probe is wrapped: a missing library or a stripped system must not
    crash the startup - the line is simply skipped.
    """
    try:
        import platform

        distro = ""
        try:
            for line in open("/etc/os-release", encoding="utf-8"):
                if line.startswith("PRETTY_NAME="):
                    distro = line.split("=", 1)[1].strip().strip('"')
                    break
        except OSError:
            pass
        print(f"[env] NeuralScreen {APP_VERSION} | {distro or 'Linux'} | "
              f"{platform.platform()}")
    except Exception:
        print(f"[env] NeuralScreen {APP_VERSION} | Linux unknown")
    try:
        # Which compositor, and whether this is Wayland at all. The single
        # most useful line in the log: nearly everything in this port
        # behaves differently between wlroots, KDE and GNOME, and the
        # answer is three environment variables away.
        session = os.environ.get("XDG_SESSION_TYPE", "?")
        desktop = (os.environ.get("XDG_CURRENT_DESKTOP")
                   or os.environ.get("DESKTOP_SESSION") or "?")
        display = os.environ.get("WAYLAND_DISPLAY") or "(none)"
        print(f"[env] session: {session} | desktop: {desktop} | "
              f"WAYLAND_DISPLAY: {display}")
    except Exception:
        pass
    try:
        import wlproto
        from wayland_shell import SHELL

        has_layer = SHELL.layer_shell is not None
        why = "" if has_layer else f" ({wlproto.reason() or 'not offered'})"
        print(f"[env] layer-shell: {'yes' if has_layer else 'no'}{why} | "
              f"outputs: {len(SHELL.outputs)}")
    except Exception:
        pass
    try:
        import portal

        print(f"[env] portal: ScreenCast v{portal._version('ScreenCast')} | "
              f"GlobalShortcuts v{portal._version('GlobalShortcuts')} | "
              f"FileChooser v{portal._version('FileChooser')}")
    except Exception:
        pass
    try:
        import gpuinfo

        g = gpuinfo.probe()
        cc = g.get("capability") or (0, 0)
        print(f"[env] GPU: {g.get('name') or 'unknown'} "
              f"({g.get('family') or '?'}, sm_{cc[0]}{cc[1]}) | "
              f"driver: {g.get('driver') or 'unknown'}")
    except Exception:
        pass
    try:
        print(f"[env] lang: {cfg.get('lang', 'en')} | "
              f"profile: {cfg.get('profile', '?')} | "
              f"work_scale: {cfg.get('work_scale', '?')} | "
              f"HDR: {'on' if cfg.get('hdr') else 'off'}")
    except Exception:
        pass


def configure(st) -> None:
    """Read the config and decide what the program is going to do.

    st.cfg_path is already set - the caller owns argparse, this module does
    not. Everything else lands on the state: the config, the NR parameters,
    the presets, the monitor and the resolution the pipeline will run at.
    """
    st.cfg = load_config(st.cfg_path)
    st.params = resolve_params(st.cfg)
    st.presets = load_presets(st.cfg)
    _apply_nr_dll(st.cfg)
    _log_environment(st.cfg)
    st.width, st.height = int(st.cfg["width"]), int(st.cfg["height"])
    monitor_cfg = st.cfg["monitor"]
    if isinstance(monitor_cfg, str):
        # New configs store the connector name ("DP-1") - resolve it to the
        # current output index; a monitor that is not connected falls back
        # to 0. A config carried over from the Windows build holds a DXGI
        # name here, which resolves to nothing and lands on the same
        # fallback - one picker prompt, then it is rewritten.
        st.monitor = resolve_output_idx(monitor_cfg)
        if st.monitor is None:
            print(f"[main] monitor {monitor_cfg!r} from config.json is not "
                  "connected - using monitor 0", file=sys.stderr)
            st.monitor = 0
    else:
        # Old configs store the positional index.
        st.monitor = int(monitor_cfg)
    st.warmup = int(st.cfg["warmup"])
    st.work_scale = float(st.cfg["work_scale"])
    # The worker reads NS_NR_SMALL once, at startup: with it on, Neural
    # Rendering runs at the work resolution and the result is scaled back up
    # instead of the network chewing the whole screen. Off by default - it is
    # faster but softer, and an update must not change how the picture looks
    # without being asked. Toggling it later restarts the worker, which is why
    # it lives in the environment rather than in the frame protocol.
    # Boost is ON unless a config says otherwise (user, 13.09). It was off
    # by default because it had been measured on still frames only; it has
    # been in a release since 1.7.0 now, and on a 5070 Ti at 4K it is
    # 45.7 -> 72.6 frames for a picture that is indistinguishable at 1:1 -
    # the residual composite puts the detail back off the native frame.
    st.nr_small = bool(st.cfg.get("nr_small", True))
    os.environ["NS_NR_SMALL"] = "1" if st.nr_small else "0"
    # The PipeWire output is the same story: the worker reads NS_SPOUT
    # once at startup, so the config flag becomes the environment before
    # the first worker is launched. Off by default - publishing the output
    # costs a full-frame copy per presented frame, and it is only useful to
    # someone recording through OBS.
    _apply_spout_env(st.cfg)
    # And HDR compatibility, read once per worker process as well.
    _apply_hdr_env(st.cfg)
    # The same for the card: NS_GPU is read once per worker process.
    _apply_gpu_env(st.cfg)
    st.lang = str(st.cfg["lang"])

    # The output resolution comes FROM THE REAL MONITOR, not from a stale
    # config.json (the monitor may have been switched to 1440p while the
    # config still remembers 4K - the overlay, the recording and the worker
    # window would start drifting away from the screen).
    st.capture = ScreenCapture(monitor_idx=st.monitor)
    st.mon_w, st.mon_h = st.capture.resolution
    if st.mon_w > 0 and st.mon_h > 0 and (st.mon_w, st.mon_h) != (st.width, st.height):
        print(f"[main] monitor {st.monitor} is {st.mon_w}x{st.mon_h} (config: {st.width}x{st.height}), "
              f"taking the real resolution")
        st.width, st.height = st.mon_w, st.mon_h
    # The worker and the overlay both need to know WHERE the chosen monitor
    # is; the resolution alone does not place anything.
    st.mon_origin = _apply_monitor_env(st.capture)

    print(f"[main] NeuralScreen - profile {st.cfg['profile']!r}, "
          f"resolution {st.width}x{st.height}, monitor {st.monitor}")
    print(f"[main] NGX parameters: {st.params}")
    print(f"[main] work_scale {st.work_scale:.2f} (NGX resolution "
          f"{int(st.width * st.work_scale)}x{int(st.height * st.work_scale)})")

    st.worker: subprocess.Popen | None = None
    st.reader: WorkerReader | None = None
    st.worker_stop: threading.Event | None = None
    st.shm: SharedFrameBuffer | None = None
    st.display: Display | None = None
    st.tray: TrayController | None = None
    st.hotkeys: HotkeyController | None = None
    st.recorder: VideoRecorder | None = None


def bring_up(st) -> None:
    """Make it exist: the worker, the overlay, the tray, the hotkeys.

    Runs inside main's try/finally - everything created here is torn down
    there, which is why the handles go onto the state as they appear rather
    than being returned in a bundle at the end.
    """
    # The worker and guides run at the work resolution (the NGX feature is
    # created from the header sizes; guides' assert requires them to match)
    st.work_w, st.work_h = _work_size(st.width, st.height, st.work_scale)
    # The v3 protocol (full_w/full_h) ONLY when work != full: at work==full
    # (scale 1.0) the worker crashes or hangs in upscale mode (verified in
    # isolation) - we use legacy full_w=0, as in D5V2.
    full_w = st.width if (st.work_w != st.width or st.work_h != st.height) else 0
    full_h = st.height if (st.work_w != st.width or st.work_h != st.height) else 0
    # Shared memory for the input frame: its size does not depend on
    # work_scale (see SharedFrameBuffer), so it is created once per process.
    st.shm = SharedFrameBuffer(st.width, st.height)
    # Which card this is and whether NR works on it. The model comes from
    # nvapi, but the support verdict comes from the worker rather than the
    # architecture: only it knows whether feature 18 was created.
    gpu_info = gpu_probe()
    st.gpu_text = gpu_describe(gpu_info)
    st.gpu_ok: bool | None = None
    st.gpu_alerted = False          # the "cannot run the pass" alert, once per verdict
    st.gpu_switch_pending = False   # set by apply_gpu: a split pipeline is worth an alert
    print(f"[main] GPU: {st.gpu_text or 'unknown'} "
          f"(group 0x{gpu_info['arch_group']:X}, officially supported: "
          f"{'yes' if gpu_info['official'] else 'no'})")
    # The stock warm-up is 120 discarded evaluations. On a fast Blackwell
    # card that is a second or two; on Turing/Ampere/Ada it can take far
    # longer than the frame watchdog, which then kills the worker on
    # frame 0 and starts a restart storm (seen on RTX 2070 at ~1 FPS and
    # on RTX 3060 Ti at ~18 FPS). Unsupported/pre-Blackwell cards get a
    # short warm-up; the actual effect is still evaluated normally
    # afterwards.
    effective_warmup = st.warmup
    if not gpu_info["official"] and st.warmup > 4:
        effective_warmup = 4
        print(f"[main] pre-Blackwell GPU: warmup {st.warmup} -> "
              f"{effective_warmup} to avoid a false frame-0 watchdog "
              f"timeout")
    # Every later (re)start has to use the same number. It used to read the
    # raw config value instead, so on a pre-Blackwell card the shortening
    # applied to the launch and to nothing else: the first revive brought
    # the 120-frame warm-up back, it outlived the 5 s watchdog, and the
    # restarts climbed to NR OFF - the exact storm the shortening exists to
    # prevent (audit F3).
    st.effective_warmup = effective_warmup
    st.worker, st.worker_logs, st.reader, st.worker_stop = start_worker(
        st.params, st.work_w, st.work_h, effective_warmup, full_w, full_h, st.shm)
    print(f"[main] worker started (pid {st.worker.pid}), header sent "
          f"({st.work_w}x{st.work_h})")

    print(f"[main] capturing monitor {st.monitor}: {st.capture.resolution}")

    st.display = Display(st.width, st.height,
                         fullscreen=bool(st.cfg["fullscreen"]),
                         output_name=getattr(st.capture, "devicename", ""))
    # The overlay is anchored to the chosen output, so it cannot land on the
    # wrong screen the way a window created at (0,0) could. The origin is
    # still needed to place a captured window's frame inside it.
    st.display.set_origin(*st.mon_origin)
    st.display.set_lang(st.lang)
    # The program draws over the desktop and gives no sign of itself -
    # without this it is unclear after launch whether it is running.
    st.startup_menu = bool(st.cfg.get("open_menu_on_start", True))
    # The before/after wipe: the share of the frame the worker leaves raw.
    st.split_pos = min(1.0, max(0.0, float(st.cfg.get("split", 0.0))))
    # The menu size, position and theme - exactly as the user left them.
    st.display.menu.set_user_scale(float(st.cfg.get("menu_scale", 1.0)))
    saved_theme = st.cfg.get("theme")
    if isinstance(saved_theme, str) and saved_theme in ("light", "dark"):
        st.display.menu.set_state({"theme": saved_theme})
    saved_offset = st.cfg.get("menu_offset")
    if isinstance(saved_offset, (list, tuple)) and len(saved_offset) == 2:
        st.display.menu.offset = [int(saved_offset[0]), int(saved_offset[1])]
    saved_height = st.cfg.get("menu_height")
    if isinstance(saved_height, (int, float)) and saved_height > 0:
        st.display.menu.user_height = int(saved_height)
    print(f"[main] output window {st.display.width}x{st.display.height}")

    # Tray icon: commands go into a queue, the main loop reads them
    st.tray_commands = queue.Queue()
    # Answers from the "Save as" dialog. The dialog is modal and lives in
    # its own thread (see _open_save_dialog); the path arrives here.
    st.shot_paths = queue.Queue()
    st.shot_dialog_open = False
    st.tray = TrayController(st.tray_commands, labels={
        "settings": UI_STRINGS[st.lang].get("settings_title", "Settings"),
        "quit": UI_STRINGS[st.lang].get("exit", "Exit"),
    })
    st.tray._set_state(nr=True, scale=st.work_scale)
    st.tray.start()
    print("[main] tray icon started")

    # The desktop entry. The Windows build opened a 1x1 APPWINDOW window
    # here to get a taskbar button, because neither the overlay nor the
    # worker window showed in the taskbar. A layer surface is not a window
    # and cannot be in a taskbar, and a decoy toplevel would give the user
    # a blank window they can focus and close - so the launcher entry is
    # what stands for "this program exists" instead. See taskbar.py.
    st.taskbar = TaskbarWindow(st.tray_commands, "NeuralScreen")
    st.taskbar.start()

    # Global hotkeys: the GlobalShortcuts portal rather than reading the
    # keyboard. The compositor routes a bound shortcut to us and not to the
    # focused application - Num1 inside a game toggles NR and the game never
    # sees the key - and unlike RegisterHotKey the user can see and change
    # every binding in their own desktop settings. Where there is no such
    # portal the fallback reads /dev/input, which does not swallow the key;
    # hotkeys.py says which path it took. The commands go into the same
    # queue the tray uses. The user's preferred bindings come from
    # config.json ("hotkeys": {"toggle": "Num1", ...}).
    hotkey_overrides = st.cfg.get("hotkeys")
    if not isinstance(hotkey_overrides, dict):
        hotkey_overrides = {}
    st.hotkey_bindings = build_bindings(hotkey_overrides)
    st.hotkeys = HotkeyController(st.tray_commands, st.hotkey_bindings)
    # The descriptions are what the user reads in their desktop's shortcuts
    # dialog, so they are the menu's own localised command names.
    # HOTKEY_ROWS already pairs every command with its i18n key, for the
    # menu's own remapping page - the same pairing the shortcuts dialog
    # needs, so it is read from there rather than duplicated.
    from overlay_ui import HOTKEY_ROWS

    st.hotkeys.start(labels={
        cmd: UI_STRINGS[st.lang].get(key, cmd) for cmd, key in HOTKEY_ROWS})
    if st.hotkeys.registered:
        print(f"[main] hotkeys registered: {', '.join(st.hotkeys.registered)} "
              f"({describe_hotkeys(st.hotkey_bindings)})")
    if st.hotkeys.failed:
        print(f"[main] the desktop bound no key for: "
              f"{', '.join(st.hotkeys.failed)}", file=sys.stderr)
    # The Num Lock warning is gone with the problem. On Windows the numpad
    # with Num Lock off sent Insert/End/arrows, indistinguishable from the
    # dedicated keys, so every numpad binding silently did nothing.
    # Wayland delivers the keycode and KP_1 is KP_1 either way - see
    # hotkeys.numlock_on, which is now a documented constant.
    #
    # The captions on the menu buttons: what the desktop ACTUALLY bound
    # wins over what the config asked for, because the compositor and the
    # user get the final say and a menu showing our wish would be lying.
    labels = hotkey_labels(st.hotkey_bindings)
    labels.update({cmd: trigger for cmd, trigger
                   in st.hotkeys.effective().items() if trigger})
    st.display.menu.set_hotkeys(labels)

    # The settings live in the overlay menu (Num2). There is no separate
    # window any more: it was a second interface over the same fields, it
    # stole focus from the game and dragged the whole of tcl/tk into the
    # runtime.

    st.guides = TemporalGuideGenerator(st.work_w, st.work_h)

    # A reused buffer: every frame allocated ~100 MB (a 4K grab plus the
    # resizes plus flow), the GC could not keep up -> OOM around frame 1900.
    # The buffer is reused through cv2.resize(dst=...). work/out buffers are
    # not needed: in v3 the full->work->full resize is done by the worker on
    # the GPU (NGX Upscaling).
    st.buf_full = np.empty((st.height, st.width, 4), dtype=np.uint8)

    st.paused = False
    # The worker died and exhausted the restart budget: the pipeline is
    # stopped (no send/recv, no more restarts) and the overlay is hidden
    # so the desktop is not covered by a black window (issue #3: black
    # screen on a GPU where feature 18 cannot be created). Cleared when
    # the user turns NR back on.
    st.worker_failed = False
    st.frame_index = 0
    st.pts = 0
    st.output_rgba = None  # the last NR frame (for a screenshot); None until the first one
    # WNDO mode: the worker shows the frame, no pixels come back to Python.
    st.want_present = bool(st.cfg.get("worker_present", True))
    st.want_motion_small = bool(st.cfg.get("motion_on_gpu", True))
    st.want_dda = bool(st.cfg.get("capture_in_worker", True))  # DDA: the worker takes the colour
    # The result pixels come back through shared memory, not the pipe.
    st.want_out_shm = bool(st.cfg.get("pixels_in_shm", True))
    # System audio ("what you hear") as a second track in the recording.
    # A config flag rather than a menu item: it is a decision made once,
    # not something to reach for while the overlay is up.
    st.record_audio = bool(st.cfg.get("record_audio", True))
    st.out_shm = False
    st.out_attempted = False
    st.motion_small = False  # the worker upscales the motion field itself
    st.motion_attempted = False  # already tried for the current worker
    st.present_mode = False      # the worker window is up right now
    st.present_attempted = False  # already tried for the current worker (do not spam)
    st.dda_mode = False          # the worker captures the screen itself
    st.dda_attempted = False     # already tried for the current worker (do not spam)
    st.window_hwnd = None        # WGCW target; None = the whole desktop (DDA1)
    st.last_foreground = 0       # the last focused window that was not ours
    st.follow_pos = None         # where the overlay currently sits (window mode)
    st.follow_resize = None      # a pending size change, waiting to settle
    # A pending MONITOR size change, same idea. follow_monitor assigns it on
    # the "nothing changed" path, so the field looked initialised - but the
    # very first call on a screen whose size already disagrees with the
    # config takes the other branch and READS it first. With __slots__ that
    # is an AttributeError, and the program leaves through main()'s
    # top-level handler (audit F2).
    st.mon_resize = None
    st.hdr_alerted = False       # the HDR notice is shown once per session
    st.mon_w, st.mon_h = st.width, st.height  # the full monitor size (for the menu layer)
    st.gray_active = False       # guides take luminance from the worker's gray channel
    st.pending_shot: Path | None = None  # a screenshot waiting for a frame with pixels
    st.recorder: VideoRecorder | None = None  # recording (Num0), MP4 AV1 NVENC
    st.work_frame = None  # the current work frame; None -> grab at the top of the loop


    st.running = True
    # Protection against rapid changes (arrow key repeat, a jerked slider):
    # the intermediate values are coalesced and only the last one is applied.
    # 0.5 s rather than 2 s: the change goes through RNSZ inside the live
    # worker process, not through a restart with an NGX init/shutdown plus
    # sleep(2) - the expensive path is only a fallback now.
    st.last_restart = 0.0
    st.pending_apply: tuple | None = None  # the deferred (scale, profile, params)
    st.next_auto_revive = 0.0      # monotonic deadline; 0 = no revive pending
    st.consecutive_restarts = 0
    st.guide_fails = 0
