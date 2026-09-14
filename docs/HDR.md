# HDR — where it stands

**Short version: the HDR path is not implemented in this build.** The switch
exists, it is off, and turning it on changes nothing except a line in the
log saying so. This page explains why, and what is kept for when it can be.

The Windows build had a working HDR path, contributed in PR #36: FP16
desktop capture, an scRGB swap chain, and the neural edit applied as a
bounded residual so untouched HDR detail survived. Every piece of that was
DXGI — `IDXGIOutput5::DuplicateOutput1` for the capture format,
`DisplayConfigGetDeviceInfo` for the SDR white level,
`RGB_FULL_G10_NONE_P709` for the presentation colour space. None of those
names means anything here, and the things that replace them are younger
than the program.

## Why it is not there yet

Three pieces have to line up, and in September 2026 they are at three
different stages.

**The protocol exists.** `color-management-v1` was merged into
wayland-protocols in February 2025 after five years and some eight hundred
review comments. It is the real thing and it is what everything below will
be built on. It is still marked as in testing, which means backward-
compatible changes are allowed against version bumps.

**Compositor support is real but uneven.** KDE Plasma 6.2+ and GNOME 48+
implement it; wlroots-based compositors are further behind, and this program
is most at home on wlroots-based compositors. So the desktop that most wants
the overlay is the one least likely to hand it an HDR surface.

**The capture side is the open question.** The portal's ScreenCast stream
carries a pixel format, and the 10-bit ones are already accepted here —
`vk_format_for_spa` maps `xBGR_210LE` and friends, and that was a deliberate
fix on Windows too (issue #58: a 10-bit output is not the same thing as HDR
being on, and refusing its format was a bug). What a stream does not yet
reliably carry is the *transfer function and primaries* that make those bits
mean HDR, nor the display's reference white. Guessing those is how a picture
comes back looking washed out with no error anywhere.

Writing a path on top of that would mean inventing the colour metadata, and
the Windows lesson this program keeps repeating is that a guess which looks
like knowledge is worse than an honest gap.

## What is kept

**The processing contract**, unchanged, because it is arithmetic rather than
API. It is the design for whenever the pieces above line up.

Let `C` be the original linear scRGB RGB, `W` the display's reference white
in scRGB units, and `P = max(0, C.r, C.g, C.b)`. The 8-bit neural input is:

```text
proxy = quantize8(linear_to_sRGB(max(C, 0) / (W + P)))
```

The resize, the neural evaluation and the residual composite operate on that
proxy. The final HDR output uses the full-size quantized input `I` and the
result `O`:

```text
delta = clamp(sRGB_to_linear(O) - sRGB_to_linear(I), -0.25, 0.25)
HDR   = clamp(C + (W + P) * delta, -65504, 65504)
```

This is an approximate way to carry an SDR neural edit into HDR. It avoids
the singularity of inverse tone mapping near 1.0 and leaves untouched HDR
detail in the original. It does not promise the artistic result of a model
trained and evaluated in HDR; strong edits can move highlights and colours.

Two properties it was chosen for, and both were verified on Windows against
the production shader: with identical proxy input and output the original
FP16 RGB values are preserved **exactly**, negative wide-gamut values
included; and NR OFF, plus the unprocessed side of the comparison wipe,
retain the raw values rather than a round-trip through the proxy.

**The switch and the hand-off.** `NS_HDR` still reaches the worker the same
way `NS_SPOUT` does — read once at process start, so toggling it restarts
the worker. The config key, the menu row and the twelve translations are all
still there. What changed is that the worker logs that it was asked and
cannot, instead of the setting appearing to work.

## What you get today

On an HDR display with HDR enabled in your compositor, the capture comes
through as whatever the portal negotiates, and the neural pass treats it as
SDR. In practice that looks washed out — which is the same symptom the
Windows README described for HDR-on-with-the-switch-off, and the same
advice applies: turn HDR off for that display while using the program.

Screenshots, recordings and any future PipeWire output are 8-bit SDR. That
was true of the Windows build with its HDR path working, and it is true
here for the simpler reason that there is no HDR path.

## What it would take

For anyone picking this up, in the order the pieces are needed:

1. **Read the stream's colour metadata** rather than assume it. PipeWire
   carries `SPA_META_VideoTransform` and format modifiers today; the
   transfer function and primaries need `color-management-v1` on the
   compositor side and a portal that passes them through.
2. **Ask the compositor for the reference white**, which is the `W` above.
   This is what `DisplayConfigGetDeviceInfo` answered on Windows and it has
   no portal equivalent yet.
3. **Keep the FP16 image end to end** in the worker. The Vulkan side is
   already most of the way there — the colour and output images are
   `R16G16B16A16_SFLOAT` precisely so the wider range exists — so this is a
   matter of not clamping into the proxy on the way through.
4. **Present through a colour-managed surface**, which is
   `color-management-v1` on our own layer surface, and is the step that
   needs a compositor that implements it.

Steps 3 and 4 are ordinary work. Steps 1 and 2 are the ones that are waiting
on the ecosystem, and they are the reason this page exists instead of a
feature.

## References

- [color-management-v1 protocol](https://wayland.app/protocols/color-management-v1)
- [Wayland colour management and HDR protocol merged (Phoronix)](https://www.phoronix.com/news/Wayland-CM-HDR-Merged)
- [Developing Wayland Color Management and HDR (Collabora)](https://www.collabora.com/news-and-blog/blog/2020/11/19/developing-wayland-color-management-and-high-dynamic-range/)
