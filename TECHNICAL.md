# NeuralScreen internals

How it works, what was measured, and why the decisions went the way
they did. For installing and using the program see
[README.md](README.md).

Every number here was measured on an RTX 5070 Ti at 4K and says so where it
matters. The numbers were taken on the Windows build, on the same card, and
are kept: the parts they measure - the network, the residual composite, the
colour conversion in the recorder - are the same code doing the same work
on the same GPU. Where the port changes what a number would be, it says so.
Where an earlier conclusion turned out to be wrong, the correction is kept
rather than quietly edited out: the mistakes are the useful part.

This is a port of a Windows program, and the interesting half of it is
which things had counterparts and which did not. **What did not come
across** is a section near the end, not a footnote.

## One window: a portal stream

`Num5` swaps the input from the whole screen to one window. On Windows that
was a swap of one capture API for another - Desktop Duplication for Windows
Graphics Capture - and both handed the worker the same kind of
`ID3D11Texture2D`, so everything downstream was shared code.

Here there is nothing to swap. A monitor and a window are both a PipeWire
node granted by the portal, and the only difference is which one the user
picked. `DDA1` and `WGCW` survive as separate messages because the pipeline
around them differs - a window's size is not the screen's, and the overlay
follows it - but the worker's side of both is `pw_stream_connect` on a node
id.

What that costs, and what it buys:

* **The user picks the window, not the program.** A Wayland client cannot
  enumerate other clients' windows, point at one, or raise it. The Windows
  build did all three: it listed every window with `EnumWindows`, took the
  one under the cursor with `WindowFromPoint`, and brought it to the front
  before capturing. None of that is possible and none of it should be. The
  compositor's own picker replaces the lot, and `restore_token` means it
  appears once rather than every launch.

* **A per-window capture is still unaffected by what is drawn on top.** The
  compositor renders the window's own content, not the screen region it
  occupies, so there is no self-capture loop - the same property the
  Windows build measured (0.0% of the captured pixels came from a
  fullscreen overlay covering the target).

* **The overlay no longer has to hide.** On Windows the whole reason this
  mode existed was that `WDA_EXCLUDEFROMCAPTURE` - needed so Desktop
  Duplication would not eat our own output - also hid the overlay from OBS
  and stopped the NVIDIA App recording anything at all (0 files out of 4
  attempts). Window mode dropped the flag to get around it. Here the flag
  has no counterpart and needs none: the overlay is a layer surface, the
  compositor composites it, and an external recorder sees exactly what the
  user sees. One whole class of workaround is gone rather than ported.

Sizes are **physical pixels**, and the worker is asked rather than guessed
at: `WGAK` carries the size the stream really produces, and the pipeline is
rebuilt for exactly that. A window's logical size on a scaled display is a
different number, and its frame is a third one. That lesson is the Windows
build's and it survived the port unchanged.

Following the window as it moves needs the compositor to say where it is,
which is a privilege and not a right. sway and Hyprland answer over their
own IPC sockets (`toplevels.py` speaks both directly rather than shelling
out to `swaymsg` or `hyprctl`); nothing else does. Where the answer is not
available the overlay stays on the screen and the picture is drawn in
place - `toplevels.geometry_source()` is what the rest of the program asks
so it can say which it got instead of silently doing nothing.

A resize reconfigures the running worker over `RNSZ` - new size, new shared
memory, same process - and still waits half a second for the size to
settle first. It used to replace the worker, which cost 1.845 s and a veil
over the picture; in place it is 0.112 s and nothing goes dark.

## Recording (Num0)

`Num0` starts/stops recording of the **NR-processed frame** into
`recordings/neuralscreen-<timestamp>.mp4`:

- AV1 NVENC hardware encoding at your desktop resolution, **60 fps**,
  quality-targeted VBR (`cq 16`, ~64 Mbps in practice, ceiling 250 Mbps),
  preset p6 + tune hq, sRGB/BT.709 color tags (metadata written both on the
  stream and on every frame — players render colors identical to the screen).
- The bitrate is a **ceiling, not a target**: on fast motion the encoder is
  allowed to spend more instead of dropping quality to hit a fixed number.
  Raising quality costs no encoding time — that is dominated by the colour
  conversion, not by the preset (measured: 1.6–1.7 s per 3 s of video at
  every setting tried).
- Recording runs at **60 fps** because the pipeline delivers ~55 frames per
  second. The previous 30 fps time base could not represent them: frames were
  squeezed into half as many ticks, which is what made fast motion fall apart
  regardless of bitrate.
- **Only the open menu is burned into the recording** — nothing else. Our
  own layer is hidden from external capture, so anything that must reach the
  file is drawn onto the frame before encoding. The same applies to
  screenshots. The HUD panel and the watermark used to be burned in as well;
  they are gone from the screen, and in a file they read as someone else's
  caption.
- Recording works in both NR ON and NR OFF (bypass) modes; the file duration
  matches real time (PTS is built from the wall clock).
- **External recorders need nothing special.** An ordinary OBS Screen
  Capture (PipeWire) source on the same monitor sees the processed picture,
  because the compositor composites the overlay rather than hiding it. On
  Windows this whole paragraph was about a Spout2 bridge, needed because
  `WDA_EXCLUDEFROMCAPTURE` hid the overlay from every screen capture; see
  "What did not come across" for the switch that survives it.
- **System audio is recorded as a second track**: the default sink's
  PipeWire monitor ("what you hear"), AAC 192 kbit/s stereo. No virtual
  cable, no microphone. Turn it off with `"record_audio": false` in
  `config.json`. A machine with no sound server still records video — the
  sound is best-effort and never stops the recording.

  The source is `@DEFAULT_MONITOR@`, which follows the default sink, so
  plugging in headphones mid-recording keeps recording. Where the server
  does not honour the alias the name is looked up with `pactl`.

  The padding for quiet stretches is kept even though the trap it was
  written for is a Windows one: WASAPI loopback handed back *no* data while
  nothing was playing, not silence, so a recorder that concatenated what it
  got ended up with audio shorter than the video. A PipeWire monitor
  produces silence at the rate it promised. The padding costs nothing when
  there is nothing to pad, and the one thing worse than an audio track that
  drifts is one that drifts only on some machines.

### What recording costs, and why it is not the bitrate

Recording used to halve the frame rate. Measured at 4K with `NS_PHASE=1`,
per frame:

```
                    idle    recording   after both fixes
frame rate          55.7      20.6           30.1 FPS
recv                17.5      31.6           24.5 ms
encode (Python)      0.4      19.9            3.1 ms
worker frame        17.4      24.1           21.3 ms
```

Two costs, neither of them the bitrate:

- **19.9 ms of RGBA→yuv420p on the CPU** plus the nvenc submit, inside the
  capture loop. Encoding now runs in its own thread behind a 4-slot queue;
  the loop only computes the PTS and hands the frame over. On a full queue
  the frame is dropped rather than stalling the loop — the user is looking at
  the screen, not at the file, and a gap does not shift timing because the
  PTS comes from the clock.
- **~7 ms of pushing 33 MB down the pipe.** The `OUTS` channel hands the
  worker a named section to write pixels into instead. The copy out of the
  section happens on the reader thread; a copy is unavoidable because the
  section has one slot and the worker overwrites it next frame, while a
  recorded frame outlives that.

Lowering the bitrate does nothing for any of this: the time goes into the
colour conversion, not into the encoder. Which is why there is no bitrate
slider in the menu — it would be a knob that looks like it helps and does
not.

## config.json

| Field | Meaning |
|---|---|
| `monitor` | the monitor to capture, by connector name (`"DP-1"`); an integer index is accepted and rewritten on first save |
| `width`, `height` | output resolution (**actual monitor resolution is used automatically when config is stale**) |
| `fullscreen` | kept for the config's shape; the overlay is a layer surface and is always the size of its output |
| `warmup` | NGX warmup frames at start |
| `work_scale` | 0.1–1.0, the resolution the network runs at, relative to the screen. Only has an effect with `nr_small` on |
| `nr_small` | process at a reduced resolution and compose the result onto the native frame: faster, sharp (the residual composite). Default `false` |
| `profile` | `Faithful`, `Natural`, `Strong / Cinematic`, `Extreme / Overdrive` |
| `intensity`, `local_tone`, `local_structure`, `skin_structure` | `null` = take from profile |
| `lang` | `en`, `ru`, `fr`, `de`, `es`, `it`, `pt`, `pl`, `uk`, `zh`, `ja`, `ko` |
| `restore_token`, `window_token` | what the portal gave back so the screen and window pickers do not appear again. Written by the app; deleting one asks you again |
| `spout` | publish the result as a PipeWire source — **not implemented in this build**, see "What did not come across" |
| `worker_present` | **has no effect**: the worker cannot present its own surface here, see Architecture |
| `motion_on_gpu` | worker upscales the motion field (`false` — CPU) |
| `capture_in_worker` | the worker reads the granted PipeWire stream itself (`false` — Python reads it through GStreamer, which is the slower fallback) |
| `pixels_in_shm` | result pixels come back through a shared section instead of the pipe (`false` — pipe, as before) |
| `split` | 0–1, share of the frame left unprocessed for the before/after wipe; 0 — off |
| `theme` | `light` / `dark` |
| `open_menu_on_start` | open the menu on launch; `false` — a short alert instead |
| `hotkeys` | `{"toggle": "Num1", ...}` — a *preference* handed to the compositor, not a binding. Names: `Num0`-`Num9`, `Numdot`, `Numplus`, `Numminus`, `Nummul`, `Numdiv`, `F1`-`F12`, `Insert`, `Home`, letters, digits, with `Ctrl+`/`Alt+`/`Shift+`. What was actually bound is what the menu shows |
| `menu_offset`, `menu_scale`, `menu_height` | where the menu sits, its scale and height. Written by the app, not meant to be edited by hand (`menu_height: null` — fit the content) |

## Architecture

Two processes, and the split is the Windows build's because the split was
never about Windows. Python drives the settings, the optical-flow guides
and the menu layer; the C++ worker owns the GPU device, the capture, NGX
and nothing else. They talk over stdin/stdout with a binary protocol whose
messages did not change:

| Message | Purpose |
|---|---|
| `D5V3` | stream header: sizes, profile, NR parameters |
| `SHMI` / `SACK` | shared-memory name for the input frame |
| `WNDO` / `WACK` | the worker presents in its own surface — **refused here**, see below |
| `MOTS` / `MACK` | motion arrives at reduced size, worker upscales it on the GPU |
| `DDA1` / `DACK` | worker reads the granted screen stream; colour never touches the CPU |
| `WGCW` / `WGAK` | the same for a window stream, by PipeWire node id |
| `GRAY` / `GAK` | worker writes downsampled luminance (320×180) back for the guides |
| `FRM1` | frame: header, then a payload or an "in shared memory" flag |
| `OUT1` | result: RGBA8 full-res, or `bytes = 0xFFFFFFFF` — it went to a section |
| `RNSZ` / `RACK` | change work resolution on the fly, no process restart |
| `OUTS` / `OAK2` | named section the worker writes result pixels into |

Two fields changed meaning rather than shape, which is why the wire format
is untouched: `WGCW`'s 64-bit handle was an `HWND` and is a PipeWire node
id, and the shared-memory names are `shm_open` names rather than
`CreateFileMapping` names. Both were always opaque to everything between
Python and the worker.

**Capture.** Python negotiates with the portal and inherits the PipeWire
remote as a file descriptor; `DDA1` tells the worker to start reading it.
The descriptor reaches the worker the only way one crosses a process
boundary here — inherited, with the number in `NS_PW_FD` — because a file
descriptor cannot travel down a pipe as an integer. A worker that could
open its own capture would be a worker that could capture without the user
having agreed.

**Shared memory.** `CreateFileMapping`/`OpenFileMapping` became
`shm_open` on both sides, which is the same shape under a different
spelling. One thing is better: the segment is unlinked as soon as both
sides have mapped it, so a crash cannot leave 33 MB in `/dev/shm`. A
Windows section could not do that either, and it is the one property worth
keeping deliberately rather than by accident.

**Guides.** The optical flow needs a small gray frame. On `GRAY` the worker
blits the colour image down and reduces it to Rec. 709 luma in integers,
then writes it into the section. No 4K frame ever crosses the CPU.

**Output.** `WNDO` is answered with a refusal, and that is a real
difference. On Windows the worker raised its own borderless D3D12-swapchain
window and presented the NGX result itself, so the pixels never returned to
Python — the overlay was reduced to a chroma-keyed menu layer on top. Two
surfaces from two processes cannot be stacked against each other on
Wayland: neither client can order itself relative to the other, and only
the compositor may. So the overlay presents, which is one surface and one
authority over what is on top. The pixels come back through the `OUTS`
section, which is the path the recorder already used.

The chroma key went with it. A layered window has one global alpha and no
per-pixel one, so HUD mode filled its background with a magenta that could
not occur in the palette and let Windows punch exactly that colour out —
with a careful note about why the panel's translucency had to come from the
window's global alpha rather than from its pixels (a blended magenta is not
the key colour, and the key does not cut out a blend; it came out as a pink
slab). A `wl_surface` carries real per-pixel alpha. The background is
transparent, the panel's own alpha does what it says, and `LWA_ALPHA`
survives only as one number folded into the alpha channel at present time.

**NR off (bypass).** `Num1` does not stop the pipeline. Frames are sent
with `FRAME_FLAG_BYPASS`: the worker blits the capture to the output
instead of evaluating. Recording and screenshots keep working, which is why
it is a copy rather than a shortcut that returns nothing.

**Recording path.** Frames are requested with `FRAME_FLAG_WANT_PIXELS`, the
open menu is drawn onto the frame with `draw_capture_overlay()`, then PyAV
encodes AV1 NVENC — unchanged, because ffmpeg's nvenc is the same encoder
on both systems.

Two constraints that look like quirks:

- **Work resolution is capped at 2560×1440.** At 4K feature 18 goes silent
  and the worker hangs on frame zero. The Windows build measured this; the
  cap is in the protocol header on both.
- **The worker no longer has to be called `nvngx.dll`.** NGX Core returned
  `FAIL_PlatformError` from `Init_Ext` for any other process name on
  Windows, which is why the Windows binary was a disguised DLL. There is no
  such check here: the binary is `neuralscreen-host` and NGX loads the
  snippet out of the application path it is given.

Scale changes go through `RNSZ` (~60 ms, the feature is recreated
in-process). If `RNSZ` fails, the pipeline falls back to a full restart.

### The overlay, flag by flag

The overlay was four Win32 extended styles. Three have exact counterparts
and they are protocol rather than flags:

| Windows | Wayland |
|---|---|
| `WS_EX_TOPMOST` | `zwlr_layer_shell_v1`, layer = overlay |
| `WS_EX_TRANSPARENT` + `WS_EX_LAYERED` | `wl_surface.set_input_region(empty)` |
| `WS_EX_NOACTIVATE` | `keyboard_interactivity = none` |
| the menu, open | `keyboard_interactivity = exclusive`, full input region |

Click-through is the clearest example of the trade. On Windows it took
three calls in a mandatory order — the style bits, then
`SetLayeredWindowAttributes` to actually activate layered mode, then
`SetWindowPos(SWP_FRAMECHANGED)` to drop the style cache — a sequence
arrived at by diagnostics, because `WS_EX_TRANSPARENT` alone does nothing.
Here it is one request, and an empty input region means exactly what it
says.

Three things that were code on Windows are now nothing at all:

* **Re-asserting topmost every thirty frames.** A borderless game asked for
  topmost too, and whoever asked last won, so the HUD vanished under
  Cyberpunk until the next re-assert — and re-asserting every frame made
  DWM flicker. A layer surface on the overlay layer is above every ordinary
  surface by protocol. There is no race to win.
* **Stealing focus with `AttachThreadInput`.** The system refuses
  `SetForegroundWindow` to a process the user has not interacted with, so
  opening the menu over a game needed the foreground thread's input state
  attached to ours to make the activation look user-initiated. A layer
  surface asks the compositor for the keyboard and gives it back; the game
  never loses its own focus.
* **`SetCapture` while dragging the panel.** Without it a drag died the
  moment the cursor left the window. Wayland grants an implicit pointer
  grab for as long as a button is down.

And one thing that was impossible is now free: the overlay is anchored to
an output rather than positioned on a virtual desktop, so it cannot land on
the wrong monitor. Issues #28, #33 and #35 were all that mistake.

## Performance

Measured on RTX 5070 Ti, 4K desktop, `work_scale` 0.5 (1920×1080), pipeline
fully on the GPU (DDA + GRAY + WNDO + MOTS). Worker-side phase breakdown
(enable with `NS_PHASE=1`):

```
                 acq   dda  upload   eval  present   frame     FPS
NR ON            0.0   0.7     0.1   16.6      0.5    17.9      55
NR OFF (bypass)  ~3    ~4      0.1      -      ~3      7.3  121-133
```

**NGX evaluation is 16.6 ms — 93% of an NR frame.** Everything else together
costs 1.3 ms, so the practical ceiling on this hardware is set by NGX, not by
the plumbing. In bypass mode NGX is skipped and the loop waits on the desktop
actually changing (`acq`), which is why it runs several times faster.

An earlier revision of this section claimed "NGX itself is ~1 ms". That was a
measurement error: the figure came from regressing round-trip time against
work resolution, and such a regression only sees the resolution-dependent part
(0.46 ms/MPix). NGX's large constant cost was invisible to it and got
attributed to transport.

The 1440p figures previously published here (64 FPS) predate the DDA fence
fix and understate current performance; they have not been re-measured.

### work_scale costs nothing (in upscale mode)

In the legacy upscale mode (nr_small off) NGX evaluation time does not depend
on the work resolution at all. Measured across the whole slider range on a 4K
desktop:

```
work          MPix   eval ms    FPS
960x540       0.52    16.13    53.8
1344x756      1.02    15.90    55.4
1728x972      1.68    15.93    56.8
2112x1188     2.51    15.96    55.7
2496x1404     3.50    15.98    55.3
```

Fit: `eval = 15.97 ms + (-0.01) ms/MPix`, R² = 0.011 — i.e. noise, not a
trend. Seven times more input pixels cost 0.2 ms, which is within the
measurement spread.

**So in upscale mode the slider is a quality control, not a speed control.**
Lowering `work_scale` buys no performance and only costs sharpness. The work
size is clamped to the 2560×1440 NGX cap anyway, so the slider always lands
exactly at the cap: 2560×1440 from a 4K desktop, 2304×1440 from 2560×1600.

Re-confirmed with D3D12 timestamps on the queue, i.e. GPU time inside
`Evaluate` rather than time around the submit, on two desktop resolutions:

```
desktop      work_scale range   work MPix      eval GPU
2560x1600    0.30 .. 1.00       0.37 .. 3.32   7.9 - 8.6 ms
3840x2160    0.30 .. 1.00       0.75 .. 3.69   15.70 - 15.73 ms
```

Five to nine times the work pixels for the same time, at either resolution.

### ...but the screen resolution does

The two rows above differ by 2.02× in screen pixels and by 1.96× in eval
time. That is the whole story: in upscale mode NGX is handed the **full-res**
frame and returns a full-res frame, downsampling to the work size internally.
So the cost is set by the desktop, not by the slider.

An earlier revision of this section concluded "the model works at its own
fixed internal resolution". That was wrong, and wrong in an instructive way:
it came from varying only `work_scale` at a single desktop resolution, which
by construction cannot see a dependence on the frame size.

Consequences: in this mode a 4K desktop pays a floor of 15.7 ms of NGX per
frame, about 47 FPS end to end, and no amount of plumbing gets near a 144 Hz
panel. On 2560×1600 the same floor is 8.0 ms.

### Processing at a reduced resolution

That floor is not a law, though. The network is **same-resolution — it
enhances, it does not upscale**, so its cost tracks the pixel count it is
handed, and "upscaling" mode hands it the whole screen. Measured in isolation
by feeding the worker different frame sizes directly:

```
work          MPix   eval GPU
1280x720      0.92    2.90 ms
1920x1080     2.07    4.60 ms
2560x1440     3.69    7.10 ms
```

Fit: `eval = 1.50 ms + 1.51 ms/MPix`, which also predicts the two numbers
above (14.0 ms at 4K, 7.7 ms at 2560×1600) and matches what the
[neural-upstream](https://github.com/matiasLombo/neural-upstream) add-on
measures for the same network in games.

So **Process at reduced resolution** (menu → speed, `"nr_small"` in
`config.json`) scales the frame down to the work resolution, runs the network
there, and scales the result back up. On a 4K desktop, work at the 2560×1440
cap:

```
                 eval GPU    FPS
full screen       16.05     42.9
reduced            7.25     65.3
```

**Off by default** (set it with the resolution slider in the menu), and the
softness that used to come with a reduced work size is gone: the result is
composed onto the pristine 1:1 native frame by the **matched residual
composite** (see below), so text and edges keep full resolution while the
cheap low-res network does the relighting.
Measured end to end on the 4K desktop: 47.9 FPS at full screen vs 71.9 FPS at
work_scale 0.65 with the composite — a 50% gain with the native anchor intact.

With it on, **Work scale** finally does something — it is the resolution the
network actually sees. With it off the slider is inert, which is exactly what
the measurements at the top of this section were showing all along.

### Matched residual composite

The composite is the DLSSNR-Cost-Scaler principle applied to the desktop:
instead of stretching the low-res network result up, the worker writes

```
result = native + (nr_out - nr_in) * strength
```

at full resolution — the neural delta (what the network changed) lands on the
pristine 1:1 native frame. The native frame stays the anchor, so text, edges
and UI keep full sharpness while the network runs at the cheap work
resolution. Measured on a synthetic detail pattern (test_residual.py):
Laplacian detail 983 with the composite vs 88 with the plain bilinear
upscale (input 951), and a hard edge 87.8 vs 59.1. `strength` is fixed at
1.0; `NS_NR_RESIDUAL=0` forces the plain upscale path for the tests.

The delta is added in **display space**, not in linear light: the textures are
`R8G8B8A8_UNORM` and the network itself works on those values, so the
composite is consistent with its input. It is worth knowing where to look if
shadows ever misbehave - a delta that is linear in code is not linear in
light, and the error is largest in the darkest pixels. Moving the composite to
linear would change the look of every scene, so it is a deliberate choice, not
an oversight.


## Before / after wipe

The **Before / after wipe** slider leaves the left share of the frame
unprocessed, so the raw capture and the NGX result sit side by side with an
accent-coloured divider between them. 0 turns it off.

It happens in the worker, on the GPU: one `CopyTextureRegion` of the left
strip of the input over the output, then the divider through
`ClearUnorderedAccessViewFloat` with a rect. Both run before Present and
before pixels are handed back, so the wipe lands in recordings and
screenshots by itself. The position rides in the top 16 bits of the frame
header's flag field, so moving the slider does not recreate the worker.

## What did not come across

Three things the Windows build had and this one does not. Each is here
because a known gap is not the same thing as a bug report.

### The architecture spoof — gone, and pre-Blackwell cards with it

`nvngx_dlssnr` refuses to create the feature on anything below Blackwell.
Its own version resource says
`NGXGpuArchitecture = NVSDK_NGX_GPU_Arch_Blackwell2`, and it carries the
message

```
DLSSNR: Unsupported GPU architecture 0x%x, minimum required 0x%x
```

The bundled build is the leaked **310.8.0** runtime: parsing its fatbin
headers shows `sm_75/86/89/120` kernels — the universal build. The refusal
on cards those kernels *can* run on is a policy check, not missing code,
which is why getting past it worked at all.

The Windows build got past it by patching one function. The library learns
the architecture through nvapi — it loads `nvapi64.dll`, takes its single
export `nvapi_QueryInterface` and asks for `NvAPI_GPU_GetArchInfo` by id.
The worker patched that function in its own process memory: prologue saved,
replaced with a jump to our handler, restored around every real call, so
NVIDIA's own code still served every GPU handle and only the returned
architecture was rewritten. It was confirmed working on a 40-series card by
a user who ran it.

The *mechanism* does not transfer — the hook depended on nvapi being a
Windows DLL with one dispatch export, and on the Windows loader's rule that
a call returns to a named module. But the *idea* has an exact Linux
counterpart, and an earlier draft of this page was wrong to say it did not.

The counterpart is `LD_PRELOAD`. On Linux the natural way to ask a card what
it is is NVML's `nvmlDeviceGetArchitecture`, which returns a small enum
(Turing 6, Ampere 7, Ada 8, Blackwell 10) — it is what `gpuinfo.py` uses and
what the function exists for. Interposing it needs no patching at all: the
loader resolves the symbol to us first, we call the real one, and we change
one number. It would also have to interpose `dlsym`, because a runtime that
`dlopen`s libnvidia-ml.so.1 and looks the symbol up on that handle walks
straight past a preload.

Two things such a shim has to be careful about, both learned from the
Windows version:

* **Rewrite the architecture and not the compute capability.** Kernel
  selection reads the latter. An Ampere card told it is sm_120 would be
  handed kernels it cannot execute — a crash inside NVIDIA's code instead of
  a clean refusal. The Windows hook rewrote the architecture only, for
  exactly this reason.
* **Claim Blackwell only for Turing, Ampere and Ada**, the three the runtime
  has kernels for. A Pascal card would get past the check and then fail with
  no kernels, which is worse than the honest refusal.

And one thing is genuinely unknown: whether the Linux NGX runtime asks
through NVML at all. The Windows hook was written against a call this
project had watched the runtime make; NVML is the most plausible candidate
here, not an observed one. So the first thing such a shim should do is log,
once, whether it was called — a run with no such line is a run where NGX
asked some other way, and that is the fact the whole question turns on.

**This is now implemented, and it is untested on hardware.** The shim is
`native/linux/ns_archspoof.c`, `build-host.sh` builds it into
`libns-archspoof.so` beside the worker, and `pipeline._add_arch_spoof` puts
it in the worker's `LD_PRELOAD` — and in nothing else's. It loads only when
`gpuinfo.probe()` reports a compute capability the bundled runtime actually
carries kernels for (7.5, 8.0, 8.6, 8.7, 8.9); a Pascal card gets the honest
refusal instead of a version check it would pass and then fail behind.
`NS_ARCH_SPOOF=0` turns it off without rebuilding.

What "untested" means precisely. The shim compiles, exports the three
symbols it must, and the decision logic has been exercised. Nobody has run
it against NVIDIA's runtime on a pre-Blackwell card, because the machine it
was written on has no NVIDIA GPU. So whether it *works* is exactly the open
question above — whether NGX asks through NVML — and the log answers it on
the first run:

| What the log says | What it means |
|---|---|
| `[spoof] … loading libns-archspoof.so` | the program decided your card qualifies |
| `[spoof] asked for the architecture: Ampere (7)` | **NGX asked through NVML.** This avenue is live |
| `[spoof] Ampere -> Blackwell` | the answer was rewritten; watch whether feature 18 now comes up |
| no `[spoof] asked` line at all | NGX asked some other way. The shim is loaded and irrelevant, and this approach is dead as written |

That last row is a real outcome and not a bug to be worked around blindly.
If it happens, the next step is to find what the runtime *does* consult, not
to widen what this file rewrites.

The menu's GPU dot stays the honest signal throughout — it goes green when
the worker actually created feature 18, not when the architecture merely
looks right. So the dot, not the absence of an error, is what says whether
any of this worked.

### PipeWire output — the switch exists, the worker does not implement it

Spout2 is a Windows mechanism: a shared DirectX texture published under a
name, read by an OBS plugin. The idea's Linux counterpart is exact — a
PipeWire video source node, which OBS reads with no plugin — and the config
key, the environment variable and the menu row all survive under their old
names so an existing config keeps working.

The worker does not publish one yet. It reads `NS_SPOUT` at startup and
logs that it was asked and cannot, rather than letting the setting appear
to do nothing.

It matters less than it did. Spout existed on Windows because the overlay
had to hide from screen capture, so an external recorder could not see the
picture at all; here an ordinary OBS Screen Capture source on the same
monitor sees exactly what the user sees.

### The worker has never run against hardware

The C++ half was rewritten from D3D11/D3D12 to Vulkan: the device, the
image imports, the NGX feature and the PipeWire consumer. It compiles, and
`neuralscreen-host --probe` fails cleanly on a machine with no NVIDIA
device. It has not been run on a machine with one.

The parts most likely to need work are named here so nobody has to find
them by bisection:

* **dmabuf import.** `Device::import_dmabuf` is written and the extensions
  are queried for, but the capture callback currently copies through
  memory instead of using it. Zero-copy needs the imported image to outlive
  the PipeWire callback, which means a fence per buffer rather than a
  memcpy. The copy is what the Windows GDI fallback cost, on a path that
  used to cost nothing.
* **Image formats.** The colour and output images are
  `R16G16B16A16_SFLOAT` on the reasoning that the feature's tone mapping
  works in a wider range than the frame arrives in. The Windows host's
  choice here was not read back before deciding.
* **The NGX resource layout.** `VK_IMAGE_LAYOUT_GENERAL` is used for
  everything NGX binds, on the grounds that it binds storage images and is
  not told what we would have preferred. If NGX wants something else it
  will say so in `FAIL_InvalidParameter` rather than silently.

## Building the worker

```
native/linux/build-host.sh
```

Needs g++ or clang++, the Vulkan headers and PipeWire's:

| Distribution | Packages |
|---|---|
| Debian / Ubuntu | `build-essential libvulkan-dev libpipewire-0.3-dev` |
| Fedora | `gcc-c++ vulkan-loader-devel pipewire-devel` |
| Arch | `base-devel vulkan-headers libpipewire` |

NGX is **not** a build dependency, which is deliberate and is also what
makes the worker buildable by anyone. The headers travel with the source
(`native/include/`), and the runtime is resolved at load time with `dlsym`
against `libnvsdk_ngx.so` or the driver's `libnvidia-ngx.so.1` —
`NS_NGX_LIB` overrides the search. The Windows build did the same thing for
a better reason than necessity: it is what makes the runtime swappable, so
a different NR build can be dropped in and pointed at with `NS_NR_DLL`
without rebuilding anything.

`native/linux/neuralscreen-host` is a build artefact and is not committed.
`neuralscreen.sh` builds it on first run.

```
native/linux/neuralscreen-host --probe
```

prints the device, whether dmabuf import is available, and whether NGX
created feature 18 — which is the fastest way to tell a driver problem from
a program problem.
