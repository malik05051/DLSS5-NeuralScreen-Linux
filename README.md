# NeuralScreen

**NVIDIA's DLSS 5 neural renderer, applied to your whole Wayland desktop in
real time.** Everything on screen — games, video, photos — goes through the
same neural network that DLSS 5 games use, and comes back sharper.

> The guide below gets you running. How it works and what was measured:
> **[TECHNICAL.md](TECHNICAL.md)**. Русская версия:
> **[README.ru.md](README.ru.md)** / **[TECHNICAL.ru.md](TECHNICAL.ru.md)**.

## How it looks

<table>
<tr>
<td><img src="https://raw.githubusercontent.com/perseval-BLR/DLSS5-NeuralScreen/main/docs/screenshot-main-light.png" alt="Menu, light theme" width="400"></td>
<td><img src="https://raw.githubusercontent.com/perseval-BLR/DLSS5-NeuralScreen/main/docs/screenshot-main-dark.png" alt="Menu, dark theme" width="400"></td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/perseval-BLR/DLSS5-NeuralScreen/main/docs/screenshot-settings.png" alt="Settings" width="400"></td>
<td><img src="https://raw.githubusercontent.com/perseval-BLR/DLSS5-NeuralScreen/main/docs/screenshot-windows.png" alt="Window list" width="400"></td>
</tr>
</table>

*One menu inside the overlay, in a light and a dark theme; the settings page;
the window list. The **Before / after wipe** slider splits the screen down the
middle so you can see what the effect is actually doing.*

## What you need

- **A Wayland session.** This is a Wayland program; it will not start on X11.

  | Compositor | Status |
  |---|---|
  | **Hyprland**, **sway**, **niri**, **Wayfire** | ✅ full overlay; window-follow works on Hyprland and sway |
  | **KDE Plasma 6** | ✅ full overlay, tray, global shortcuts |
  | **GNOME 46+** | ⚠️ works, but the overlay is an ordinary window: it takes focus, it is not click-through, and it will not stay over a fullscreen game. Mutter does not implement layer-shell. |

- **An NVIDIA RTX card:**

  | Cards | Status |
  |---|---|
  | **RTX 50** (Blackwell) | ✅ works |
  | **RTX 40** / **RTX 30** / **RTX 20** | ✅ works — verified on an RTX 3050 Laptop GPU. NGX refuses the feature below Blackwell, a policy check the runtime's own kernels (sm_75/86/89) contradict; on Linux the answer it gets comes from dxvk-nvapi, and `DXVK_NVAPI_GPU_ARCH=GB200` is all it takes. See [TECHNICAL.md](TECHNICAL.md), "The neural renderer runs through Proton". |

- **Proton.** The neural renderer itself is a Windows D3D12 library —
  NVIDIA has published no Linux build of it, and this program checked
  (see TECHNICAL.md). So the network runs in a small Windows process under
  [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom) with
  vkd3d-proton underneath; everything else — capture, overlay, menu — is
  native. You need a GE-Proton in `~/.local/share/Steam/compatibilitytools.d`
  (Steam, or ProtonUp-Qt), `umu-launcher`, `mingw-w64-gcc`, and the driver's
  Wine NGX (`/usr/lib/nvidia/wine`, part of the driver package). Then:

  ```bash
  ./native/proton/setup.sh
  ```

  builds the Windows side, prepares the Wine prefix once, and ends with a
  verdict — `feature 18 (neural renderer) WORKS through Proton` — or the
  reason it does not.

- **The proprietary NVIDIA driver, current.** Not a formality: the neural
  runtime talks to it directly, and an old driver is the commonest reason it
  refuses to start or the picture never appears. Nouveau will not do.
- **xdg-desktop-portal** with your desktop's backend, and **PipeWire**. The
  capture, the hotkeys and the file dialogs all come through the portal.
- **Python 3.10+** and a handful of modules — the launcher names them and
  the exact command if any are missing.

## Install

1. Download the archive from [Releases](https://github.com/perseval-BLR/DLSS5-NeuralScreen/releases)
   and unpack it anywhere. NVIDIA's runtime (`native/proton/nvngx_dlssnr.dll`)
   is inside; from a git checkout, copy it there yourself from the Windows
   project's archive.
2. Run **`./native/proton/setup.sh`** once — the Proton side, see above.
3. Run **`./neuralscreen.sh`**. The first run builds the worker (a few
   seconds, needs `g++`, `libvulkan-dev` and `libpipewire-0.3-dev`) and
   writes a launcher entry so the program shows up in your application menu.

There is no installer: to remove the program, delete the folder and run
`./neuralscreen.sh --uninstall` first to take the launcher entry and the
autostart file back out.

**The first launch asks you two things, once:** a picker for the screen to
capture, and a dialog for the hotkeys. That is Wayland's security model, not
a missing feature — and it really is once, the program remembers what the
portal gives back.

> **Do not use it in competitive online games.** A fullscreen overlay over a
> game is what anti-cheat systems look for.

## Using it

The program sits in the tray and draws over your desktop. Press **Num2** for
the menu.

| Key | What it does |
|---|---|
| **Num2** | open / close the menu |
| **Num1** | neural rendering on / off |
| **Num3** | screenshot |
| **Num0** | start / stop recording, with sound |
| **Num4** / **Num6** | processing resolution down / up |
| **Num5** | capture one window |
| **Ctrl+Alt+Q** | quit |

These are *preferences*, not commands: your compositor decides what the keys
actually are and the menu shows what it chose. Change them under the sliders
icon or in your desktop's own shortcut settings — both work. Num Lock makes
no difference, unlike on Windows.

While the menu is open it takes the mouse and keyboard, so it works on top of
a game; closed, clicks go straight through it.

### Whole screen or one window

The whole screen is the default. **Source**, at the top of the menu, switches
between **Fullscreen** and **Window mode**. Choosing the second opens your
compositor's window picker — a client cannot capture another client's window
without you saying so. **Num5** does the same from the keyboard.

The overlay follows the window as it moves on **Hyprland** and **sway**,
which tell clients where windows are; elsewhere it stays on the screen and
the window's picture is drawn in place. Resizing reconfigures the worker in
place, with no black moment.

## The menu

The dot next to your graphics card is green when neural rendering is really
running on it, red when it is not.

- **Source** — the whole screen or one window, and which window.
- **Profile** — how strong the effect is, from *Faithful* to *Extreme*;
  *Natural* by default. The four sliders underneath are the same thing in
  detail. **Save preset** stores the current values under a name and puts it
  in the Profile list; **Delete preset** removes it. Dark scenes are
  brightened automatically so shadows keep their detail.
- **Before / after wipe** — leaves the left part unprocessed so you can see
  what the effect is doing. Back to 0 when done.
- **Boost** — on by default. The network runs at a reduced resolution, and a
  slider chooses which: measured on a 5070 Ti at 4K, **45.7 → 72.6 frames**
  at the default step and **83.4** at the lowest. The picture stays sharp —
  the result is composed onto your original frame, so text and edges keep
  full resolution. Turn it off to compare.

Everything else is behind the sliders icon: which monitor is processed and
which card does it, the screenshot folder, PipeWire
output, the recording indicator, leaving an unchanged screen alone, opening
the menu on launch, autostart, the key assignments, the theme — and the
language, of which there are **12**: English, Russian, French, German,
Spanish, Italian, Portuguese, Polish, Ukrainian, Chinese, Japanese and
Korean.

## Recording and screenshots

**Num0** records what you see, with system sound, into an MP4 in your Videos
folder. **Num3** saves a screenshot. The menu appears in both if it is open,
on purpose. A red dot with a timer sits in the corner while recording (it can
be turned off in the settings).

Screenshots open your desktop's own Save dialog; set **Screenshot folder...**
in the settings once and it will start there every time.

**Recording externally:** an ordinary OBS Screen Capture (PipeWire) source on
the same monitor sees the processed picture — the compositor composites the
overlay rather than hiding it. On Windows the overlay had to hide from screen
capture and OBS needed a Spout2 plugin to see anything.

The **PipeWire output (OBS)** switch is the successor to that Spout2 bridge.
**It is not implemented in this build** — the switch is there and the worker
says so in the log. Use the Screen Capture source or Num0.

## If something is not working

**Nothing appears after launch.** Check `NeuralScreen.log` next to the
program — it names the cause. The `[env]` line at the top says which
compositor you are on and whether it has a layer shell; start there.

**The overlay is behind everything, or steals focus.** Your compositor has no
layer-shell (GNOME), and the `[env]` line says so. That is the fallback
working, not a fault.

**No hotkeys at all.** Your portal backend ships no GlobalShortcuts (plain
wlroots and niri). Adding yourself to the `input` group and logging back in
enables the direct fallback; the tray icon opens the menu either way.

**No tray icon on GNOME.** GNOME needs the AppIndicator extension. The menu
is still on Num2.

**A game covers the overlay.** True fullscreen hands the screen to one
client. Switch the game to *borderless* — same as on Windows, same reason.

**The picture is soft.** Turn *Boost* off, or move its slider up a step.

**Everything is washed out.** HDR is on for that display and the neural pass
is treating it as SDR. Turn HDR off for that display — the **HDR
compatibility** switch is not implemented in this build, and
[docs/HDR.md](docs/HDR.md) says what it is waiting on.

## Known limitations

- **The network runs through Proton**, not natively: NVIDIA ships it only
  for D3D12. Per frame that is one GPU readback and one upload on each
  side; on an RTX 3050 Laptop GPU the pipeline runs at 24–25 fps with the
  network at 1248×702 and a 1080p desktop. A faster card or a smaller
  processing resolution moves that number.
- **GNOME** gets a downgraded overlay — Mutter implements no layer-shell.
- **Window-follow needs Hyprland or sway.** No other compositor tells a
  client where another client's window is, and none should.
- **True fullscreen games** cannot have an overlay drawn over them.
- **HDR displays are not handled.** The Windows build had a working HDR
  path; the Wayland pieces it needs are only half-arrived. See
  [docs/HDR.md](docs/HDR.md).
- **Pipeline latency** is 40–60 ms (17–20 ms with Boost) — fine
  interactively, not competitively; **processing resolution is capped at
  2560×1440**, output is always your full native resolution.

## License

The code here is MIT. NVIDIA's `nvngx_dlssnr.dll` is the leaked 310.8.0
runtime (sm_75/86/89/120 kernels), never in this repository, shipped in the
release archive as-is, no guarantees, research-only. Interface faces: IBM Plex (OFL-1.1, `fonts/OFL.txt`).
