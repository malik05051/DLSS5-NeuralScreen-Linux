// neuralscreen-host - the half of NeuralScreen that talks to the GPU.
//
// Python owns the decisions: which monitor, which profile, when to restart,
// what the menu says. This process owns one thing - running the neural
// renderer on a frame - and everything it knows arrives down its stdin as
// the fixed-size messages in ns_ipc.h. That split is the Windows build's and
// is unchanged; it is also the reason the port was possible at all, because
// the protocol never mentioned Windows.
//
// What it does, in order:
//
//   1. Picks a Vulkan device (NS_GPU) and initialises NGX on it.
//   2. Connects to the PipeWire remote the portal granted, inherited as a
//      file descriptor (NS_PW_FD) with the node id in NS_PW_NODE.
//   3. Reads messages until the pipe closes:
//        DV5  the header - work size, input size, the four sliders
//        FMR1 a frame; the pixels come from shared memory, from the pipe,
//             or from the capture itself depending on the flags
//        SHMI/GRAY/OUTS  open a shared section by name
//        RNSZ reconfigure in place, no restart
//        DDA1 read the granted stream instead of being sent pixels
//        WGCW the same, for a window stream by node id
//        MOTS the motion field arrives at flow size, upscale it here
//        WNDO present the result in the worker's own surface
//   4. Answers each with its acknowledgement and each frame with OUT1.
//
//   neuralscreen-host --live     serve Python over stdin/stdout
//   neuralscreen-host --probe    print the device and NGX verdict, exit
//
// Logs to NeuralScreen-host.log next to the binary, and to stderr, which is
// where Python's own log picks it up.

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include "ns_ipc.h"
#include "ns_proton.h"
#include "ns_pw.h"
#include "ns_vk.h"

namespace ns {

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

namespace {
FILE *g_log = nullptr;
std::mutex g_log_mutex;
}  // namespace

void log(const char *fmt, ...)
{
    std::lock_guard<std::mutex> guard(g_log_mutex);
    char line[1024];
    va_list args;
    va_start(args, fmt);
    vsnprintf(line, sizeof(line), fmt, args);
    va_end(args);
    // stderr as well as the file: Python drains it into NeuralScreen.log, so
    // a user pastes one log and it has both halves of the program in it.
    fprintf(stderr, "%s\n", line);
    fflush(stderr);
    if (g_log != nullptr) {
        fprintf(g_log, "%s\n", line);
        fflush(g_log);
    }
}

namespace {

void open_log(const char *argv0)
{
    std::string path(argv0 ? argv0 : "neuralscreen-host");
    const size_t slash = path.rfind('/');
    path = (slash == std::string::npos) ? std::string(".")
                                        : path.substr(0, slash);
    path += "/NeuralScreen-host.log";
    g_log = fopen(path.c_str(), "a");
}

int env_int(const char *name, int fallback)
{
    const char *value = getenv(name);
    if (value == nullptr || *value == '\0') return fallback;
    char *end = nullptr;
    const long parsed = strtol(value, &end, 10);
    return (end != nullptr && *end == '\0') ? static_cast<int>(parsed)
                                            : fallback;
}

bool env_flag(const char *name)
{
    const char *value = getenv(name);
    return value != nullptr && strcmp(value, "1") == 0;
}

std::string exe_dir(const char *argv0)
{
    std::string path(argv0 ? argv0 : ".");
    const size_t slash = path.rfind('/');
    return (slash == std::string::npos) ? std::string(".")
                                        : path.substr(0, slash);
}

// --- stdio --------------------------------------------------------------

bool read_exact(void *dst, size_t size)
{
    auto *out = static_cast<uint8_t *>(dst);
    size_t done = 0;
    while (done < size) {
        const ssize_t n = ::read(STDIN_FILENO, out + done, size - done);
        if (n == 0) return false;              // Python closed the pipe
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        done += static_cast<size_t>(n);
    }
    return true;
}

bool write_exact(const void *src, size_t size)
{
    const auto *in = static_cast<const uint8_t *>(src);
    size_t done = 0;
    while (done < size) {
        const ssize_t n = ::write(STDOUT_FILENO, in + done, size - done);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) continue;
            return false;
        }
        done += static_cast<size_t>(n);
    }
    return true;
}

// --- POSIX shared memory ------------------------------------------------
//
// The Windows side of this was OpenFileMappingA on a name Python had made
// with CreateFileMapping. shm_open is the same shape: Python creates it,
// passes the name down the pipe, this opens it read-write and maps it. The
// name is NUL-padded to 64 bytes on the wire, so it is taken up to the
// first NUL rather than by the whole field.

class Shm {
public:
    ~Shm() { close(); }

    bool open(const char name[64], size_t size)
    {
        close();
        char safe[65];
        memcpy(safe, name, 64);
        safe[64] = '\0';
        name_.assign(safe);
        const int fd = ::shm_open(name_.c_str(), O_RDWR, 0600);
        if (fd < 0) {
            log("[shm] cannot open %s: %s", name_.c_str(), strerror(errno));
            return false;
        }
        struct stat info {};
        if (fstat(fd, &info) == 0 && static_cast<size_t>(info.st_size) < size)
            size = static_cast<size_t>(info.st_size);
        void *mapped = ::mmap(nullptr, size, PROT_READ | PROT_WRITE,
                              MAP_SHARED, fd, 0);
        ::close(fd);
        if (mapped == MAP_FAILED) {
            log("[shm] cannot map %s: %s", name_.c_str(), strerror(errno));
            return false;
        }
        base_ = static_cast<uint8_t *>(mapped);
        size_ = size;
        return true;
    }

    void close()
    {
        if (base_ != nullptr) {
            ::munmap(base_, size_);
            base_ = nullptr;
        }
        size_ = 0;
        name_.clear();
    }

    uint8_t *data() const { return base_; }
    size_t size() const { return size_; }
    bool valid() const { return base_ != nullptr; }

private:
    uint8_t *base_ = nullptr;
    size_t size_ = 0;
    std::string name_;
};

// ---------------------------------------------------------------------------
// The host
// ---------------------------------------------------------------------------

struct Host {
    Device device;
    Ngx ngx;
    ProtonNr proton;
    PwCapture capture;
    std::string exe_dir;
    // Which side runs the network. Native NGX is tried first and would win
    // the day NVIDIA ships a Linux snippet; until then it is the Proton
    // process, and the log says so once.
    bool via_proton = false;
    bool backend_logged = false;

    // Sizes. `work` is what the network runs at, `full` what comes in.
    uint32_t work_w = 0, work_h = 0;
    uint32_t full_w = 0, full_h = 0;
    NrOptions options;
    uint32_t warmup = 0;
    bool nr_small = false;

    // Images. `color` and `output` are at the io size; `motion` at work.
    Image color, output, motion, imported;
    Buffer upload, download, motion_upload;

    // Shared sections.
    Shm frame_shm;          // SHMI: colour + motion in, one slot
    size_t color_capacity = 0, motion_capacity = 0;
    Shm gray_shm;           // GRAY: luminance back to Python for the guides
    uint32_t gray_w = 0, gray_h = 0;
    Shm out_shm;            // OUTS: the result pixels back, with a seqlock
    uint32_t out_w = 0, out_h = 0;
    uint64_t out_seq = 0;

    // Capture state.
    bool capture_active = false;
    uint32_t motion_src_w = 0, motion_src_h = 0;   // MOTS: the flow size

    // The newest captured frame, handed over by the PipeWire thread.
    std::mutex capture_mutex;
    std::vector<uint8_t> capture_pixels;
    uint32_t capture_w = 0, capture_h = 0;
    uint32_t capture_stride = 0;
    VkFormat capture_format = VK_FORMAT_UNDEFINED;
    bool capture_fresh = false;

    uint64_t frames_done = 0;

    bool io_size(uint32_t *w, uint32_t *h) const
    {
        const bool upscale = full_w > 0 && full_h > 0
                             && (full_w != work_w || full_h != work_h);
        *w = upscale ? full_w : work_w;
        *h = upscale ? full_h : work_h;
        return upscale;
    }

    bool build_resources();
    void release_resources();
    bool ensure_feature();
    uint32_t nr_result() const { return via_proton ? proton.last_result() : ngx.last_result(); }
    void release_nr_feature() { ngx.release_feature(device); proton.release_feature(device); }

    bool take_capture_frame();
    bool upload_colour(const uint8_t *pixels, size_t bytes);
    bool upload_motion(const uint8_t *halves, size_t bytes);
    bool run_frame(const NsFrame &frame, uint32_t *ngx_result);
    void write_gray();
    bool write_out(const NsFrame &frame, bool ok, uint32_t ngx_result);
};

bool Host::build_resources()
{
    uint32_t io_w = 0, io_h = 0;
    io_size(&io_w, &io_h);
    if (io_w == 0 || io_h == 0) return false;

    // R16G16B16A16_SFLOAT rather than an 8-bit format: the feature's own
    // tone mapping works in a wider range than the frame arrives in, and an
    // 8-bit intermediate is where the banding in dark scenes came from.
    // The frame is uploaded as 8-bit and converted by the blit.
    if (!device.make_image(color, io_w, io_h, VK_FORMAT_R16G16B16A16_SFLOAT,
                           VK_IMAGE_USAGE_STORAGE_BIT)) {
        log("[host] could not create the colour image %ux%u", io_w, io_h);
        return false;
    }
    if (!device.make_image(output, io_w, io_h, VK_FORMAT_R16G16B16A16_SFLOAT,
                           VK_IMAGE_USAGE_STORAGE_BIT)) {
        log("[host] could not create the output image %ux%u", io_w, io_h);
        return false;
    }
    // The motion field: two half floats per pixel at the work resolution.
    if (!device.make_image(motion, work_w, work_h, VK_FORMAT_R16G16_SFLOAT,
                           VK_IMAGE_USAGE_STORAGE_BIT)) {
        log("[host] could not create the motion image %ux%u", work_w, work_h);
        return false;
    }
    if (!device.make_buffer(upload, static_cast<VkDeviceSize>(io_w) * io_h * 8,
                            VK_BUFFER_USAGE_TRANSFER_SRC_BIT, true)
        || !device.make_buffer(download,
                               static_cast<VkDeviceSize>(io_w) * io_h * 8,
                               VK_BUFFER_USAGE_TRANSFER_DST_BIT, true)
        || !device.make_buffer(motion_upload,
                               static_cast<VkDeviceSize>(WORK_MAX_W)
                                   * WORK_MAX_H * 4,
                               VK_BUFFER_USAGE_TRANSFER_SRC_BIT, true)) {
        log("[host] could not create the staging buffers");
        return false;
    }
    return true;
}

void Host::release_resources()
{
    device.destroy_image(color);
    device.destroy_image(output);
    device.destroy_image(motion);
    device.destroy_image(imported);
    device.destroy_buffer(upload);
    device.destroy_buffer(download);
    device.destroy_buffer(motion_upload);
}

bool Host::ensure_feature()
{
    if (ngx.has_feature() || proton.has_feature()) return true;
    if (ngx.ready() && ngx.create_feature(device, work_w, work_h, full_w, full_h)) {
        via_proton = false;
        if (!backend_logged) log("[host] the neural pass runs natively through NGX");
        backend_logged = true;
        return true;
    }
    const uint32_t native_result = ngx.ready() ? ngx.last_result() : 0;
    std::string error;
    if (!proton.running() && !proton.start(exe_dir, &error)) {
        if (!backend_logged) {
            log("[host] native NGX has no feature 18 (0x%08X) and %s", native_result, error.c_str());
        }
        backend_logged = true;
        via_proton = false;
        return false;
    }
    if (!proton.create_feature(device, work_w, work_h, full_w, full_h)) return false;
    via_proton = true;
    if (!backend_logged) {
        log("[host] the neural pass runs through Proton (native NGX answered 0x%08X for feature 18)",
            native_result);
    }
    backend_logged = true;
    return true;
}

// --- pixels in ------------------------------------------------------------

bool Host::upload_colour(const uint8_t *pixels, size_t bytes)
{
    uint32_t io_w = 0, io_h = 0;
    io_size(&io_w, &io_h);
    const size_t want = static_cast<size_t>(io_w) * io_h * 4;
    if (pixels == nullptr || bytes < want || !upload.valid()) return false;

    // An RGBA8 staging image, blitted into the float colour image. The blit
    // is what converts the format; doing it with a copy would mean writing a
    // compute shader to do what the hardware does for free.
    Image staging;
    if (!device.make_image(staging, io_w, io_h, VK_FORMAT_R8G8B8A8_UNORM,
                           VK_IMAGE_USAGE_TRANSFER_DST_BIT)) {
        return false;
    }
    memcpy(upload.mapped, pixels, want);

    VkCommandBuffer cmd = device.begin();
    if (cmd == VK_NULL_HANDLE) {
        device.destroy_image(staging);
        return false;
    }
    device.copy_buffer_to_image(cmd, upload, staging);
    device.blit(cmd, staging, color, false);
    const bool ok = device.submit_and_wait(cmd);
    device.destroy_image(staging);
    return ok;
}

bool Host::upload_motion(const uint8_t *halves, size_t bytes)
{
    if (halves == nullptr || !motion_upload.valid()) return false;
    // MOTS: the field may arrive at the optical-flow resolution and be
    // upscaled here. The Windows host did this on the GPU for the same
    // reason - a 320x180 field stretched on the CPU was 7 ms of the frame.
    const uint32_t src_w = motion_src_w ? motion_src_w : work_w;
    const uint32_t src_h = motion_src_h ? motion_src_h : work_h;
    const size_t want = static_cast<size_t>(src_w) * src_h * 4;
    if (bytes < want) return false;
    memcpy(motion_upload.mapped, halves, want);

    Image staging;
    if (!device.make_image(staging, src_w, src_h, VK_FORMAT_R16G16_SFLOAT,
                           VK_IMAGE_USAGE_TRANSFER_DST_BIT)) {
        return false;
    }
    VkCommandBuffer cmd = device.begin();
    if (cmd == VK_NULL_HANDLE) {
        device.destroy_image(staging);
        return false;
    }
    device.copy_buffer_to_image(cmd, motion_upload, staging);
    // Linear, because a motion field is a continuous quantity: nearest here
    // put a visible block grid into fast pans.
    device.blit(cmd, staging, motion, true);
    const bool ok = device.submit_and_wait(cmd);
    device.destroy_image(staging);
    return ok;
}

bool Host::take_capture_frame()
{
    std::vector<uint8_t> pixels;
    uint32_t w = 0, h = 0, stride = 0;
    VkFormat format = VK_FORMAT_UNDEFINED;
    {
        std::lock_guard<std::mutex> guard(capture_mutex);
        if (!capture_fresh || capture_pixels.empty()) return false;
        pixels.swap(capture_pixels);
        w = capture_w;
        h = capture_h;
        stride = capture_stride;
        format = capture_format;
        capture_fresh = false;
    }
    if (w == 0 || h == 0) return false;

    Image staging;
    if (!device.make_image(staging, w, h, format,
                           VK_IMAGE_USAGE_TRANSFER_DST_BIT)) {
        return false;
    }
    const size_t tight = static_cast<size_t>(w) * h * 4;
    if (upload.size < tight) {
        device.destroy_image(staging);
        return false;
    }
    // Row by row when the compositor's stride is padded, in one go when it
    // is not - which it usually is not, and the common case should not pay
    // for the uncommon one.
    if (stride == w * 4) {
        memcpy(upload.mapped, pixels.data(), tight);
    } else {
        auto *dst = static_cast<uint8_t *>(upload.mapped);
        for (uint32_t y = 0; y < h; ++y) {
            memcpy(dst + static_cast<size_t>(y) * w * 4,
                   pixels.data() + static_cast<size_t>(y) * stride, w * 4);
        }
    }
    VkCommandBuffer cmd = device.begin();
    if (cmd == VK_NULL_HANDLE) {
        device.destroy_image(staging);
        return false;
    }
    device.copy_buffer_to_image(cmd, upload, staging);
    device.blit(cmd, staging, color, false);
    const bool ok = device.submit_and_wait(cmd);
    device.destroy_image(staging);
    return ok;
}

// --- pixels out -----------------------------------------------------------

void Host::write_gray()
{
    // The reverse channel: a luminance downsample for the optical flow on
    // the Python side. Small enough (320x180 is the flow size) that the
    // Windows host wrote it with a plain memcpy and no seqlock, and the same
    // judgement holds - one torn flow frame is invisible and the next one
    // fixes it.
    if (!gray_shm.valid() || gray_w == 0 || gray_h == 0) return;
    Image small;
    if (!device.make_image(small, gray_w, gray_h, VK_FORMAT_R8G8B8A8_UNORM,
                           VK_IMAGE_USAGE_TRANSFER_DST_BIT)) {
        return;
    }
    Buffer readback;
    if (!device.make_buffer(readback,
                            static_cast<VkDeviceSize>(gray_w) * gray_h * 4,
                            VK_BUFFER_USAGE_TRANSFER_DST_BIT, true)) {
        device.destroy_image(small);
        return;
    }
    VkCommandBuffer cmd = device.begin();
    if (cmd != VK_NULL_HANDLE) {
        device.blit(cmd, color, small, true);
        device.copy_image_to_buffer(cmd, small, readback);
        if (device.submit_and_wait(cmd)) {
            const auto *src = static_cast<const uint8_t *>(readback.mapped);
            uint8_t *dst = gray_shm.data();
            const size_t count =
                std::min<size_t>(gray_shm.size(),
                                 static_cast<size_t>(gray_w) * gray_h);
            // Rec. 709 luma, in integers: the flow only cares about
            // structure, and a float conversion here would cost more than
            // the flow itself.
            for (size_t i = 0; i < count; ++i) {
                const uint8_t *p = src + i * 4;
                dst[i] = static_cast<uint8_t>(
                    (54u * p[0] + 183u * p[1] + 19u * p[2]) >> 8);
            }
        }
    }
    device.destroy_buffer(readback);
    device.destroy_image(small);
}

bool Host::write_out(const NsFrame &frame, bool ok, uint32_t ngx_result)
{
    uint32_t io_w = 0, io_h = 0;
    io_size(&io_w, &io_h);
    const bool want_pixels = (frame.flags & FRAME_FLAG_WANT_PIXELS) != 0
                             || !capture_active;
    const size_t tight = static_cast<size_t>(io_w) * io_h * 4;

    std::vector<uint8_t> pixels;
    if (ok && want_pixels) {
        Image staging;
        if (device.make_image(staging, io_w, io_h, VK_FORMAT_R8G8B8A8_UNORM,
                              VK_IMAGE_USAGE_TRANSFER_SRC_BIT)) {
            VkCommandBuffer cmd = device.begin();
            if (cmd != VK_NULL_HANDLE) {
                device.blit(cmd, output, staging, false);
                device.copy_image_to_buffer(cmd, staging, download);
                if (device.submit_and_wait(cmd) && download.mapped != nullptr) {
                    pixels.resize(tight);
                    memcpy(pixels.data(), download.mapped, tight);
                }
            }
            device.destroy_image(staging);
        }
    }

    NsOut out{};
    out.magic = OUT_MAGIC;
    out.index = frame.index;
    out.ok = ok ? 1u : 0u;
    out.ngx_result = ngx_result;
    out.pts = frame.pts;

    if (!pixels.empty() && out_shm.valid() && out_w == io_w && out_h == io_h) {
        // The OUTS section: the pixels go there and only the header travels
        // down the pipe. On Windows this saved ~7 ms per 4K frame of pushing
        // 33 MB through a pipe, and the arithmetic has not changed.
        //
        // The seqlock is the contract: odd while writing, even when done,
        // and Python retries a torn read. The two stores around the memcpy
        // must not be reordered past it, which is what the fences are for -
        // the Windows host relied on x86's store ordering and got away with
        // it; being explicit costs nothing.
        uint8_t *base = out_shm.data();
        uint64_t seq = out_seq + 1;
        memcpy(base, &seq, sizeof(seq));
        __atomic_thread_fence(__ATOMIC_RELEASE);
        const size_t room = out_shm.size() > 8 ? out_shm.size() - 8 : 0;
        memcpy(base + 8, pixels.data(), std::min(room, pixels.size()));
        __atomic_thread_fence(__ATOMIC_RELEASE);
        seq += 1;
        memcpy(base, &seq, sizeof(seq));
        out_seq = seq;
        out.bytes = OUT_BYTES_IN_SHM;
        return write_exact(&out, sizeof(out));
    }

    out.bytes = static_cast<uint32_t>(pixels.size());
    if (!write_exact(&out, sizeof(out))) return false;
    if (!pixels.empty()) return write_exact(pixels.data(), pixels.size());
    return true;
}

// --- one frame ------------------------------------------------------------

bool Host::run_frame(const NsFrame &frame, uint32_t *ngx_result)
{
    *ngx_result = 0;

    // Nothing changed and Python says so: the picture on screen is already
    // right, and re-running the network on an identical frame is the one
    // cost with no benefit at all.
    if (frame.flags & FRAME_FLAG_SKIP_STATIC) {
        *ngx_result = nr_result();
        return true;
    }

    if (frame.flags & FRAME_FLAG_BYPASS) {
        // NR OFF: the capture goes straight to the output. Recording and
        // screenshots still work, which is why this is a copy rather than a
        // shortcut that returns no pixels.
        VkCommandBuffer cmd = device.begin();
        if (cmd == VK_NULL_HANDLE) return false;
        device.blit(cmd, color, output, false);
        return device.submit_and_wait(cmd);
    }

    if (!ensure_feature()) {
        *ngx_result = nr_result();
        return false;
    }
    if (via_proton) {
        const bool ok = proton.evaluate(device, color, output, motion, options,
                                        frame.reset != 0);
        *ngx_result = proton.last_result();
        return ok;
    }
    VkCommandBuffer cmd = device.begin();
    if (cmd == VK_NULL_HANDLE) return false;
    const bool ok = ngx.evaluate(device, cmd, color, output, motion, options,
                                 frame.reset != 0);
    *ngx_result = ngx.last_result();
    const bool submitted = device.submit_and_wait(cmd);
    return ok && submitted;
}

// ---------------------------------------------------------------------------
// The message loop
// ---------------------------------------------------------------------------

void send_ack(uint32_t magic, uint32_t ok, uint32_t a = 0, uint32_t b = 0,
              int64_t pts = 0)
{
    NsAck ack{};
    ack.magic = magic;
    ack.ok = ok;
    ack.a = a;
    ack.b = b;
    ack.pts = pts;
    write_exact(&ack, sizeof(ack));
}

void apply_header(Host &host, const NsHeader &header)
{
    host.work_w = header.width;
    host.work_h = header.height;
    host.full_w = header.full_w;
    host.full_h = header.full_h;
    host.warmup = header.warmup;
    host.options.profile = header.profile;
    host.options.preset = header.preset;
    host.options.style = header.style;
    host.options.auto_mask = header.auto_mask;
    host.options.ui_correction = header.ui_correction;
    host.options.intensity = header.intensity;
    host.options.local_tone = header.local_tone;
    host.options.local_structure = header.local_structure;
    host.options.skin_structure = header.skin_structure;
}

int run(Host &host)
{
    bool have_header = false;

    for (;;) {
        uint32_t magic = 0;
        if (!read_exact(&magic, sizeof(magic))) {
            log("[host] the pipe closed - exiting");
            return 0;
        }

        if (magic == VIDEO_MAGIC || magic == RESIZE_MAGIC) {
            NsHeader header{};
            header.magic = magic;
            if (!read_exact(reinterpret_cast<uint8_t *>(&header) + 4,
                            sizeof(header) - 4)) {
                return 1;
            }
            const bool resize = magic == RESIZE_MAGIC;
            apply_header(host, header);
            host.nr_small = resize
                                ? (header.frame_count & RESIZE_FLAG_NR_SMALL) != 0
                                : host.nr_small;
            host.release_nr_feature();
            host.release_resources();
            const bool built = host.build_resources() && host.ensure_feature();
            if (resize) {
                // RACK carries the NGX result, which is what Python logs
                // when a reconfigure fails - a reconfigure that silently
                // did nothing was how a resize used to end in a black
                // screen.
                send_ack(RESIZE_ACK_MAGIC, built ? 1u : 0u,
                         host.nr_result(), 0, 0);
            }
            log("[host] %s: work %ux%u, io %ux%u -> %s",
                resize ? "reconfigured" : "header", host.work_w, host.work_h,
                host.full_w, host.full_h, built ? "ready" : "FAILED");
            have_header = true;
            continue;
        }

        if (magic == SHM_MAGIC) {
            NsNamed msg{};
            msg.magic = magic;
            if (!read_exact(reinterpret_cast<uint8_t *>(&msg) + 4,
                            sizeof(msg) - 4)) {
                return 1;
            }
            host.color_capacity = msg.width_or_color_bytes;
            host.motion_capacity = msg.height_or_motion_bytes;
            const bool ok = host.frame_shm.open(
                msg.name, host.color_capacity + host.motion_capacity);
            send_ack(SHM_ACK_MAGIC, ok ? 1u : 0u);
            log("[host] frame section %s: %.1f MB",
                ok ? "open" : "REFUSED",
                (host.color_capacity + host.motion_capacity) / 1e6);
            continue;
        }

        if (magic == GRAY_MAGIC) {
            NsNamed msg{};
            msg.magic = magic;
            if (!read_exact(reinterpret_cast<uint8_t *>(&msg) + 4,
                            sizeof(msg) - 4)) {
                return 1;
            }
            host.gray_w = msg.width_or_color_bytes;
            host.gray_h = msg.height_or_motion_bytes;
            const bool ok = host.gray_shm.open(
                msg.name, static_cast<size_t>(host.gray_w) * host.gray_h);
            send_ack(GRAY_ACK_MAGIC, ok ? 1u : 0u);
            continue;
        }

        if (magic == OUTS_MAGIC) {
            NsNamed msg{};
            msg.magic = magic;
            if (!read_exact(reinterpret_cast<uint8_t *>(&msg) + 4,
                            sizeof(msg) - 4)) {
                return 1;
            }
            host.out_w = msg.width_or_color_bytes;
            host.out_h = msg.height_or_motion_bytes;
            const bool ok = host.out_shm.open(
                msg.name,
                static_cast<size_t>(host.out_w) * host.out_h * 4 + 8);
            host.out_seq = 0;
            send_ack(OUTS_ACK_MAGIC, ok ? 1u : 0u);
            continue;
        }

        if (magic == MOTION_MAGIC) {
            NsSimple msg{};
            msg.magic = magic;
            if (!read_exact(reinterpret_cast<uint8_t *>(&msg) + 4,
                            sizeof(msg) - 4)) {
                return 1;
            }
            host.motion_src_w = msg.width;
            host.motion_src_h = msg.height;
            send_ack(MOTION_ACK_MAGIC, 1u);
            log("[host] motion field arrives at %ux%u, upscaled on the GPU",
                msg.width, msg.height);
            continue;
        }

        if (magic == DDA_MAGIC || magic == WGC_MAGIC) {
            uint64_t node = 0;
            uint32_t width = 0, height = 0;
            if (magic == WGC_MAGIC) {
                NsWgc msg{};
                msg.magic = magic;
                if (!read_exact(reinterpret_cast<uint8_t *>(&msg) + 4,
                                sizeof(msg) - 4)) {
                    return 1;
                }
                node = msg.node;
                width = msg.width;
                height = msg.height;
            } else {
                NsSimple msg{};
                msg.magic = magic;
                if (!read_exact(reinterpret_cast<uint8_t *>(&msg) + 4,
                                sizeof(msg) - 4)) {
                    return 1;
                }
                width = msg.width;
                height = msg.height;
                node = static_cast<uint64_t>(env_int("NS_PW_NODE", -1));
            }

            if (width == 0 && height == 0) {
                // Turning the capture off: back to being sent frames.
                host.capture.stop();
                host.capture_active = false;
                send_ack(magic == WGC_MAGIC ? WGC_ACK_MAGIC : DDA_ACK_MAGIC,
                         1u);
                continue;
            }

            const int fd = env_int("NS_PW_FD", -1);
            bool ok = false;
            if (fd >= 0 && node != static_cast<uint64_t>(-1)) {
                std::string error;
                Host *self = &host;
                ok = host.capture.start(
                    fd, static_cast<uint32_t>(node),
                    [self](const PwFrame &frame) {
                        // Copied out on PipeWire's thread and picked up by
                        // the loop. The zero-copy dmabuf import belongs
                        // here too, and is the next thing to land: it needs
                        // the imported image to outlive this callback,
                        // which means a fence per buffer rather than a
                        // memcpy. Until then the copy is what the Windows
                        // GDI fallback cost, on a path that used to cost
                        // nothing.
                        if (frame.pixels == nullptr) return;
                        std::lock_guard<std::mutex> guard(self->capture_mutex);
                        const size_t bytes =
                            static_cast<size_t>(frame.stride) * frame.height;
                        self->capture_pixels.assign(frame.pixels,
                                                    frame.pixels + bytes);
                        self->capture_w = frame.width;
                        self->capture_h = frame.height;
                        self->capture_stride = frame.stride;
                        self->capture_format = frame.format;
                        self->capture_fresh = true;
                    },
                    &error);
                if (!ok) log("[host] capture refused: %s", error.c_str());
            } else {
                log("[host] no PipeWire remote (NS_PW_FD=%d, node=%lld) - "
                    "frames must come from Python", fd,
                    static_cast<long long>(node));
            }
            host.capture_active = ok;

            uint32_t aw = width, ah = height;
            if (ok) {
                // Wait briefly for the format: WGAK has to carry the real
                // capture size, and Python rebuilds the pipeline for
                // exactly that number. Guessing it from the window's
                // geometry is the mistake the Windows build documented.
                for (int i = 0; i < 100 && aw == 0; ++i) {
                    std::this_thread::sleep_for(
                        std::chrono::milliseconds(10));
                    host.capture.size(&aw, &ah);
                }
                if (aw == 0) host.capture.size(&aw, &ah);
            }
            send_ack(magic == WGC_MAGIC ? WGC_ACK_MAGIC : DDA_ACK_MAGIC,
                     ok ? 1u : 0u, aw, ah);
            continue;
        }

        if (magic == WINDOW_MAGIC) {
            NsSimple msg{};
            msg.magic = magic;
            if (!read_exact(reinterpret_cast<uint8_t *>(&msg) + 4,
                            sizeof(msg) - 4)) {
                return 1;
            }
            // WNDO asked the Windows host to open its own always-on-top
            // window and present into it, so the 33 MB round trip through
            // Python could be skipped. It is refused here: a second
            // Wayland surface next to the overlay would have to be stacked
            // against it, and a client cannot order itself against another
            // client. The overlay presents, which is one surface and one
            // authority over what is on top.
            send_ack(WINDOW_ACK_MAGIC, 0u);
            continue;
        }

        if (magic == FRAME_MAGIC) {
            NsFrame frame{};
            frame.magic = magic;
            if (!read_exact(reinterpret_cast<uint8_t *>(&frame) + 4,
                            sizeof(frame) - 4)) {
                return 1;
            }
            if (!have_header) {
                log("[host] a frame arrived before the header - ignored");
                continue;
            }

            uint32_t io_w = 0, io_h = 0;
            host.io_size(&io_w, &io_h);
            const size_t colour_bytes = static_cast<size_t>(io_w) * io_h * 4;
            const uint32_t src_w = host.motion_src_w ? host.motion_src_w
                                                     : host.work_w;
            const uint32_t src_h = host.motion_src_h ? host.motion_src_h
                                                     : host.work_h;
            const size_t motion_bytes = static_cast<size_t>(src_w) * src_h * 4;

            bool have_colour = false;
            if (frame.flags & FRAME_FLAG_NO_COLOR) {
                have_colour = host.take_capture_frame();
                if (!have_colour) {
                    // No new frame from the compositor: the screen has not
                    // changed. The last one is still in the colour image,
                    // which is exactly what the duplication path's
                    // WAIT_TIMEOUT meant.
                    have_colour = host.color.valid();
                }
            } else if (frame.flags & FRAME_FLAG_SHM) {
                if (host.frame_shm.valid()) {
                    have_colour = host.upload_colour(host.frame_shm.data(),
                                                     colour_bytes);
                    host.upload_motion(
                        host.frame_shm.data() + host.color_capacity,
                        motion_bytes);
                }
            } else {
                std::vector<uint8_t> buffer(colour_bytes + motion_bytes);
                if (!read_exact(buffer.data(), buffer.size())) return 1;
                have_colour = host.upload_colour(buffer.data(), colour_bytes);
                host.upload_motion(buffer.data() + colour_bytes, motion_bytes);
            }

            uint32_t ngx_result = 0;
            const bool ok = have_colour && host.run_frame(frame, &ngx_result);
            if (ok) {
                ++host.frames_done;
                host.write_gray();
            }
            if (!host.write_out(frame, ok, ngx_result)) return 1;
            continue;
        }

        log("[host] unknown message 0x%08X - the stream is out of step",
            magic);
        return 1;
    }
}

// The Proton route: is it set up, and does the DLL create and evaluate
// feature 18 on this card through it? A real evaluation, timed, because
// "created" alone is not the answer the user needs.
bool probe_proton(Host &host)
{
    std::string exe, wine, prefix, describe, error;
    const bool located = ProtonNr::locate(host.exe_dir, &exe, &wine, &prefix, &describe);
    printf("\nproton: %s\n", describe.c_str());
    if (!located) {
        printf("proton verdict: not set up - see native/proton/README.md\n");
        return false;
    }
    if (!host.proton.start(host.exe_dir, &error)) {
        printf("proton verdict: %s\n", error.c_str());
        return false;
    }
    host.work_w = 1280; host.work_h = 720; host.full_w = 0; host.full_h = 0;
    if (!host.build_resources()) { printf("proton verdict: could not build test images\n"); return false; }
    const bool created = host.proton.create_feature(host.device, 1280, 720, 0, 0);
    printf("proton feature 18: %s (0x%08X %s, %u ms)\n", created ? "created" : "refused",
           host.proton.last_result(), ngx_result_name(host.proton.last_result()),
           host.proton.last_millis());
    if (!created) return false;
    bool ok = true;
    for (int i = 0; i < 5 && ok; ++i) {
        ok = host.proton.evaluate(host.device, host.color, host.output, host.motion, host.options, i == 0);
        printf("proton evaluate %d: %s (0x%08X, %u ms on the GPU)\n", i, ok ? "ok" : "FAILED",
               host.proton.last_result(), host.proton.last_millis());
    }
    printf("\nproton verdict: feature 18 (neural renderer) %s through Proton at 1280x720\n",
           ok ? "WORKS" : "does not work");
    host.proton.release_feature(host.device);
    host.release_resources();
    return ok;
}

int probe(Host &host)
{
    printf("device: %s\n", host.device.name().c_str());
    printf("dmabuf import: %s\n", host.device.has_dmabuf() ? "yes" : "no");
    printf("ngx: %s (0x%08X %s)\n", host.ngx.ready() ? "ready" : "unavailable",
           host.ngx.last_result(), ngx_result_name(host.ngx.last_result()));
    if (!host.ngx.ready()) return probe_proton(host) ? 0 : 2;

    // What NGX itself says it has snippets for.
    static const char *const kCaps[] = {
        "SuperSampling.Available", "SuperSampling.NeedsUpdatedDriver",
        "SuperSampling.FeatureInitResult",
        "SuperSamplingDenoising.Available",
        "SuperSamplingDenoising.FeatureInitResult",
        "FrameGeneration.Available", "FrameGeneration.FeatureInitResult",
        "ImageSuperResolution.Available", "VideoSuperResolution.Available",
        "DeepDVC.Available", "DeepResolve.Available",
        "ImageSignalProcessing.Available", "InPainting.Available",
        "SlowMotion.Available",
    };
    for (const char *name : kCaps) {
        int value = 0;
        if (host.ngx.capability(name, &value))
            printf("cap %-44s = %d\n", name, value);
        else
            printf("cap %-44s   (not reported)\n", name);
    }

    // Then ask for every feature id directly. This is the real test: the
    // capability map only knows the names the SDK headers know.
    bool nr = false;
    for (uint32_t id = 0; id < 32; ++id) {
        const uint32_t r = host.ngx.probe_feature(host.device, id, 1280, 720);
        const char *verdict = r == 0 ? "CREATED" :
            (r == 0xBAD0000B || r == 0xBAD00001) ? "no snippet" : "present?";
        printf("feature %2u: 0x%08X %-32s %s\n", id, r, ngx_result_name(r),
               verdict);
        if (id == 18 && r == 0) nr = true;
    }
    printf("\nnative verdict: feature 18 (neural renderer) %s\n",
           nr ? "available" : "not available on this driver");
    if (nr) return 0;
    return probe_proton(host) ? 0 : 2;
}

}  // namespace
}  // namespace ns

int main(int argc, char **argv)
{
    using namespace ns;
    open_log(argv[0]);

    bool live = false, want_probe = false;
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--live") == 0) live = true;
        else if (strcmp(argv[i], "--probe") == 0) want_probe = true;
    }
    if (!live && !want_probe) {
        fprintf(stderr, "usage: %s --live | --probe\n", argv[0]);
        return 2;
    }

    Host host;
    std::string error;
    if (!host.device.create(env_int("NS_GPU", -1), &error)) {
        log("[host] %s", error.c_str());
        return 3;
    }
    // NGX loads nvngx_dlssnr.so out of the application path it is given, so
    // the path is where the snippet is - next to this binary, or wherever
    // NS_NR_DLL points for a swapped-in build.
    std::string snippet_dir = exe_dir(argv[0]);
    host.exe_dir = snippet_dir;
    if (const char *override_path = getenv("NS_NR_DLL")) {
        const std::string value(override_path);
        const size_t slash = value.rfind('/');
        if (slash != std::string::npos) snippet_dir = value.substr(0, slash);
    }
    if (!host.ngx.init(host.device, snippet_dir, &error)) {
        log("[host] %s", error.c_str());
        log("[host] native NGX is unavailable; the neural pass will use "
            "Proton if native/proton is set up");
    }
    if (env_flag("NS_SPOUT")) {
        // The PipeWire output node: publishing the result so OBS can read
        // it. Not wired up yet - the flag is read here so the log says so
        // rather than the setting appearing to do nothing.
        log("[host] PipeWire output was requested but this build does not "
            "publish one yet - record with the built-in recorder (Num0)");
    }

    const int rc = want_probe ? probe(host) : run(host);
    host.capture.stop();
    host.release_nr_feature();
    host.proton.stop();
    host.release_resources();
    host.ngx.shutdown(host.device);
    host.device.destroy();
    return rc;
}
