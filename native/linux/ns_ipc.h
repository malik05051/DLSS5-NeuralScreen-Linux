// The worker protocol, as the host reads it.
//
// The definitive statement of these formats is protocol.py: Python packs
// them, the host unpacks them, and every field below has a struct.pack
// format string next to it there. They are byte-packed little-endian with
// no alignment padding, which is what "<4Iq" means and what the pragma
// below enforces - a compiler that aligned the int64 to eight bytes would
// read every message one field out of place.
//
// Nothing here changed in the move off Windows. The formats are the same,
// the magics are the same, and the acknowledgements are the same, because
// the split they describe - Python decides, the worker runs the network -
// is not a Windows idea. Two fields changed meaning rather than shape and
// say so where they are declared: WGC's 64-bit handle is a PipeWire node id
// rather than an HWND, and the shared-memory names are shm_open names
// rather than CreateFileMapping names.

#pragma once
#include <cstdint>

#pragma pack(push, 1)

// --- Python -> host -------------------------------------------------------

static const uint32_t VIDEO_MAGIC  = 0x33563544u;  // 'DV5' v3, the header
static const uint32_t FRAME_MAGIC  = 0x314D5246u;  // 'FMR1'
static const uint32_t SHM_MAGIC    = 0x494D4853u;  // 'SHMI'
static const uint32_t MOTION_MAGIC = 0x53544F4Du;  // 'MOTS'
static const uint32_t WINDOW_MAGIC = 0x4F444E57u;  // 'WNDO'
static const uint32_t RESIZE_MAGIC = 0x5A534E52u;  // 'RNSZ'
static const uint32_t DDA_MAGIC    = 0x31414444u;  // 'DDA1'
static const uint32_t WGC_MAGIC    = 0x57434757u;  // 'WGCW'
static const uint32_t OUTS_MAGIC   = 0x5354554Fu;  // 'OUTS'
static const uint32_t GRAY_MAGIC   = 0x59415247u;  // 'GRAY'

// --- host -> Python -------------------------------------------------------

static const uint32_t OUT_MAGIC        = 0x3154554Fu;  // 'OUT1'
static const uint32_t SHM_ACK_MAGIC    = 0x4B434153u;  // 'SACK'
static const uint32_t MOTION_ACK_MAGIC = 0x4B43414Du;  // 'MACK'
static const uint32_t WINDOW_ACK_MAGIC = 0x4B434157u;  // 'WACK'
static const uint32_t RESIZE_ACK_MAGIC = 0x4B434152u;  // 'RACK'
static const uint32_t DDA_ACK_MAGIC    = 0x4B434144u;  // 'DACK'
static const uint32_t WGC_ACK_MAGIC    = 0x4B414757u;  // 'WGAK'
static const uint32_t OUTS_ACK_MAGIC   = 0x324B414Fu;  // 'OAK2'
static const uint32_t GRAY_ACK_MAGIC   = 0x4B434147u;  // 'GAK'

// --- frame flags ----------------------------------------------------------

enum FrameFlags : uint32_t {
    FRAME_FLAG_SHM          = 0x01,  // pixels are in the shared section
    FRAME_FLAG_WANT_PIXELS  = 0x02,  // return them even in window mode
    FRAME_FLAG_MOTION_SMALL = 0x04,  // motion at flow size, upscale on the GPU
    FRAME_FLAG_NO_COLOR     = 0x08,  // the host captures; no colour was sent
    FRAME_FLAG_BYPASS       = 0x10,  // NR OFF: show the capture, skip NGX
    FRAME_FLAG_SPLIT        = 0x20,  // before/after wipe, share in bits 16..31
    FRAME_FLAG_SKIP_STATIC  = 0x40,  // nothing changed - idle, do not re-run
};

static const uint32_t RESIZE_FLAG_NR_SMALL  = 0x01;
static const uint32_t WINDOW_FLAG_CAPTURABLE = 0x01;
static const uint32_t WINDOW_FLAG_DISABLE    = 0x02;

// The sentinel in OUT1's `bytes` field meaning "the pixels are in the OUTS
// section, not behind this header".
static const uint32_t OUT_BYTES_IN_SHM = 0xFFFFFFFFu;

// The cap the work resolution is clamped to. NGX goes silent above it; the
// number is the Windows build's, measured rather than guessed.
static const uint32_t WORK_MAX_W = 2560;
static const uint32_t WORK_MAX_H = 1440;

// --- messages -------------------------------------------------------------

// "<10I4f2I" - the header, and RNSZ with a different magic.
struct NsHeader {
    uint32_t magic;
    uint32_t width, height;        // the work resolution (the NGX feature)
    uint32_t warmup;
    uint32_t frame_count;          // 0 = run until the pipe closes
    uint32_t profile, preset, style, auto_mask, ui_correction;
    float    intensity, local_tone, local_structure, skin_structure;
    uint32_t full_w, full_h;       // the input size; 0 = 1:1, no upscale
};

// "<4Iq"
struct NsFrame {
    uint32_t magic;
    uint32_t index;
    uint32_t reset;                // 1 = drop the temporal history
    uint32_t flags;                // FrameFlags, split share in bits 16..31
    int64_t  pts;
};

// "<5Iq"
struct NsOut {
    uint32_t magic;
    uint32_t index;
    uint32_t ok;
    uint32_t bytes;                // OUT_BYTES_IN_SHM when it went to a section
    uint32_t ngx_result;
    int64_t  pts;
};

// "<4Iq64s" - SHMI, GRAY and OUTS all share this shape. `name` is an
// shm_open name: one leading slash, NUL-padded to 64 bytes.
struct NsNamed {
    uint32_t magic;
    uint32_t width_or_color_bytes;
    uint32_t height_or_motion_bytes;
    uint32_t flags;
    int64_t  pts;
    char     name[64];
};

// "<4Iq" - the shape every acknowledgement uses. The two middle fields
// carry whatever that particular ack has to say: WGAK puts the real capture
// size there, RACK puts the NGX result.
struct NsAck {
    uint32_t magic;
    uint32_t ok;
    uint32_t a;
    uint32_t b;
    int64_t  pts;
};

// "<4IqQ" - WGCW. The 64-bit field was an HWND on Windows and is a PipeWire
// node id here: the portal grants a window as a stream, and a node id is
// what a stream is called.
struct NsWgc {
    uint32_t magic;
    uint32_t width, height;
    uint32_t flags;
    int64_t  pts;
    uint64_t node;
};

// "<4Iq" - DDA1, MOTS and WNDO.
struct NsSimple {
    uint32_t magic;
    uint32_t width, height;
    uint32_t flags;
    int64_t  pts;
};

#pragma pack(pop)

static_assert(sizeof(NsHeader) == 64, "NsHeader must match HEADER_FMT");
static_assert(sizeof(NsFrame) == 24, "NsFrame must match FRAME_FMT");
static_assert(sizeof(NsOut) == 28, "NsOut must match OUT_FMT");
static_assert(sizeof(NsNamed) == 88, "NsNamed must match SHM_FMT");
static_assert(sizeof(NsAck) == 24, "NsAck must match SHM_ACK_FMT");
static_assert(sizeof(NsWgc) == 32, "NsWgc must match WGC_FMT");
static_assert(sizeof(NsSimple) == 24, "NsSimple must match DDA_FMT");
