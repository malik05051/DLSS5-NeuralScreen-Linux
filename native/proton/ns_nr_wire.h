// The wire between neuralscreen-host (Linux, Vulkan) and nvngx.dll_nr.exe
// (Windows, D3D12, under Proton). Included by both sides, so the layouts are
// written once. Everything is little-endian and packed by hand: fixed-size
// structs of uint32/float/int64 only, no padding surprises across compilers.
//
// Control goes down the exe's stdin/stdout; pixels live in one shared file
// mapping (a file on /dev/shm the host creates and the exe opens through
// Wine's Z: drive). Layout of the mapping, all offsets from its start:
//
//   [0, color_bytes)             RGBA8 colour, io_w x io_h, tightly packed
//   [motion_off, +motion_bytes)  RG16F motion, work_w x work_h
//   [output_off, +output_bytes)  RGBA8 result, io_w x io_h
//
// io = full when upscaling (full_w/full_h set and different from work),
// else work - the same rule as Host::io_size.
#pragma once
#include <stdint.h>

#define NS_NR_MAGIC_CREATE  0x4448524Eu   // "NRHD" host -> exe: (re)create the feature
#define NS_NR_MAGIC_CREATED 0x4B41524Eu   // "NRAK" exe -> host
#define NS_NR_MAGIC_FRAME   0x5246524Eu   // "NRFR" host -> exe: evaluate the mapping
#define NS_NR_MAGIC_DONE    0x4B4F524Eu   // "NROK" exe -> host
#define NS_NR_MAGIC_HELLO   0x4C48524Eu   // "NRHL" exe -> host, once at startup

struct NsNrCreate {
    uint32_t magic;          // NS_NR_MAGIC_CREATE
    uint32_t work_w, work_h; // the network's resolution
    uint32_t full_w, full_h; // io resolution when upscaling, else 0
    uint32_t preset;         // DLSSNR.Hint.Render.Preset
    uint32_t reserved[2];
};                           // 32 bytes

struct NsNrFrame {
    uint32_t magic;          // NS_NR_MAGIC_FRAME
    uint32_t seq;
    uint32_t reset;
    uint32_t style, auto_mask, ui_correction;
    float intensity, local_tone, local_structure, skin_structure, exposure;
    uint32_t reserved[5];
};                           // 64 bytes

struct NsNrReply {
    uint32_t magic;          // NRHL / NRAK / NROK
    uint32_t seq;            // the frame's seq; 0 for create/hello
    uint32_t ok;             // 1 on success
    uint32_t ngx_result;     // the raw NGX result of the last call
    uint32_t millis;         // wall time of the call, for the log
    uint32_t reserved;
};                           // 24 bytes

static inline void ns_nr_layout(uint32_t work_w, uint32_t work_h,
                                uint32_t full_w, uint32_t full_h,
                                uint32_t *io_w, uint32_t *io_h,
                                uint64_t *motion_off, uint64_t *output_off,
                                uint64_t *total)
{
    const int upscale = full_w > 0 && full_h > 0
                        && (full_w != work_w || full_h != work_h);
    *io_w = upscale ? full_w : work_w;
    *io_h = upscale ? full_h : work_h;
    const uint64_t color = (uint64_t)*io_w * *io_h * 4;
    const uint64_t motion = (uint64_t)work_w * work_h * 4;
    *motion_off = color;
    *output_off = color + motion;
    *total = color + motion + color;
}
