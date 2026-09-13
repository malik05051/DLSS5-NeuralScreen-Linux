#include "ns_pw.h"

#include <cstring>
#include <unistd.h>

#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <spa/debug/types.h>
#include <spa/param/video/type-info.h>

namespace ns {

extern void log(const char *fmt, ...);

VkFormat vk_format_for_spa(uint32_t format)
{
    // The compositor offers what it has; these four cover every Wayland
    // compositor in practice. BGRx is the overwhelmingly common one -
    // it is what a 32-bit XRGB8888 buffer is called here - and it maps to
    // Vulkan's B8G8R8A8, with the unused byte read as alpha and ignored
    // downstream because the frame is opaque.
    switch (format) {
    case SPA_VIDEO_FORMAT_BGRA:
    case SPA_VIDEO_FORMAT_BGRx:
        return VK_FORMAT_B8G8R8A8_UNORM;
    case SPA_VIDEO_FORMAT_RGBA:
    case SPA_VIDEO_FORMAT_RGBx:
        return VK_FORMAT_R8G8B8A8_UNORM;
    case SPA_VIDEO_FORMAT_ABGR_210LE:
    case SPA_VIDEO_FORMAT_xBGR_210LE:
        // 10-bit output. The Windows build learned the hard way that a
        // 10-bit desktop is not the same thing as HDR being on (issue #58):
        // this format is accepted on its own merits, and whether the HDR
        // path runs is NS_HDR's business.
        return VK_FORMAT_A2B10G10R10_UNORM_PACK32;
    case SPA_VIDEO_FORMAT_ARGB_210LE:
    case SPA_VIDEO_FORMAT_xRGB_210LE:
        return VK_FORMAT_A2R10G10B10_UNORM_PACK32;
    default:
        return VK_FORMAT_UNDEFINED;
    }
}

struct PwCapture::Impl {
    pw_thread_loop *loop = nullptr;
    pw_context *context = nullptr;
    pw_core *core = nullptr;
    pw_stream *stream = nullptr;
    spa_hook stream_listener{};
    spa_video_info_raw format{};
    bool have_format = false;
    PwCapture::Sink sink;
    PwCapture *owner = nullptr;
    mutable std::mutex mutex;
    uint32_t width = 0, height = 0;
};

namespace {

void on_state_changed(void *data, pw_stream_state old, pw_stream_state state,
                      const char *error)
{
    (void)data;
    log("[pw] stream %s -> %s%s%s", pw_stream_state_as_string(old),
        pw_stream_state_as_string(state), error ? ": " : "",
        error ? error : "");
}

void on_param_changed(void *data, uint32_t id, const spa_pod *param)
{
    auto *impl = static_cast<PwCapture::Impl *>(data);
    if (param == nullptr || id != SPA_PARAM_Format) return;

    uint32_t media_type = 0, media_subtype = 0;
    if (spa_format_parse(param, &media_type, &media_subtype) < 0) return;
    if (media_type != SPA_MEDIA_TYPE_video
        || media_subtype != SPA_MEDIA_SUBTYPE_raw) {
        return;
    }
    if (spa_format_video_raw_parse(param, &impl->format) < 0) return;
    {
        std::lock_guard<std::mutex> guard(impl->mutex);
        impl->have_format = true;
        impl->width = impl->format.size.width;
        impl->height = impl->format.size.height;
    }
    log("[pw] format: %ux%u %s, modifier 0x%llx", impl->format.size.width,
        impl->format.size.height,
        spa_debug_type_find_name(spa_type_video_format, impl->format.format),
        static_cast<unsigned long long>(impl->format.modifier));

    // Announce how many buffers we want and that a dmabuf is acceptable.
    // Without the buffer param the compositor picks its own count, which is
    // usually fine; saying so explicitly keeps the queue short, and a short
    // queue is what keeps the latency where TECHNICAL.md says it is.
    uint8_t buffer[512];
    spa_pod_builder builder = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    const spa_pod *params[1];
    params[0] = static_cast<const spa_pod *>(spa_pod_builder_add_object(
        &builder,
        SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(3, 2, 8),
        SPA_PARAM_BUFFERS_dataType,
        SPA_POD_CHOICE_FLAGS_Int((1 << SPA_DATA_DmaBuf)
                                 | (1 << SPA_DATA_MemFd)
                                 | (1 << SPA_DATA_MemPtr))));
    pw_stream_update_params(impl->stream, params, 1);
}

void on_process(void *data)
{
    auto *impl = static_cast<PwCapture::Impl *>(data);
    pw_buffer *b = pw_stream_dequeue_buffer(impl->stream);
    if (b == nullptr) return;
    // Always dequeue the newest: a frame we are too slow for is a frame the
    // user has already stopped looking at. The Windows path made the same
    // call with the duplication API's AcquireNextFrame timeout.
    while (pw_buffer *newer = pw_stream_dequeue_buffer(impl->stream)) {
        pw_stream_queue_buffer(impl->stream, b);
        b = newer;
    }

    spa_buffer *buf = b->buffer;
    if (buf->n_datas == 0) {
        pw_stream_queue_buffer(impl->stream, b);
        return;
    }

    PwFrame frame;
    {
        std::lock_guard<std::mutex> guard(impl->mutex);
        if (!impl->have_format) {
            pw_stream_queue_buffer(impl->stream, b);
            return;
        }
        frame.width = impl->width;
        frame.height = impl->height;
        frame.format = vk_format_for_spa(impl->format.format);
        frame.modifier = impl->format.modifier;
    }

    spa_data &first = buf->datas[0];
    if (first.type == SPA_DATA_DmaBuf) {
        frame.is_dmabuf = true;
        frame.planes.reserve(buf->n_datas);
        for (uint32_t i = 0; i < buf->n_datas; ++i) {
            DmabufPlane plane;
            // Duplicated, because Vulkan closes what it imports and
            // PipeWire owns the original for as long as the buffer lives.
            plane.fd = ::dup(static_cast<int>(buf->datas[i].fd));
            plane.offset = buf->datas[i].chunk->offset;
            plane.stride = static_cast<uint32_t>(buf->datas[i].chunk->stride);
            frame.planes.push_back(plane);
        }
    } else if (first.data != nullptr) {
        frame.pixels = static_cast<const uint8_t *>(first.data);
        frame.stride = static_cast<uint32_t>(first.chunk->stride);
        frame.size = first.chunk->size;
    } else {
        pw_stream_queue_buffer(impl->stream, b);
        return;
    }

    if (impl->sink) impl->sink(frame);
    if (impl->owner != nullptr) impl->owner->note_frame();

    // The descriptors are Vulkan's now if it took them; anything left
    // unclaimed is closed here rather than leaked into a long-running
    // process that dups three of them per frame.
    for (DmabufPlane &plane : frame.planes) {
        if (plane.fd >= 0) ::close(plane.fd);
    }
    pw_stream_queue_buffer(impl->stream, b);
}

const pw_stream_events kStreamEvents = {
    .version = PW_VERSION_STREAM_EVENTS,
    .destroy = nullptr,
    .state_changed = on_state_changed,
    .control_info = nullptr,
    .io_changed = nullptr,
    .param_changed = on_param_changed,
    .add_buffer = nullptr,
    .remove_buffer = nullptr,
    .process = on_process,
    .drained = nullptr,
    .command = nullptr,
    .trigger_done = nullptr,
};

}  // namespace

PwCapture::~PwCapture() { stop(); }

bool PwCapture::start(int fd, uint32_t node, Sink sink, std::string *error)
{
    stop();
    static bool initialised = false;
    if (!initialised) {
        pw_init(nullptr, nullptr);
        initialised = true;
    }
    impl_ = new Impl();
    impl_->sink = std::move(sink);
    impl_->owner = this;

    impl_->loop = pw_thread_loop_new("ns-capture", nullptr);
    if (impl_->loop == nullptr) {
        if (error) *error = "pw_thread_loop_new failed";
        stop();
        return false;
    }
    pw_thread_loop_lock(impl_->loop);
    impl_->context = pw_context_new(pw_thread_loop_get_loop(impl_->loop),
                                    nullptr, 0);
    if (impl_->context == nullptr) {
        pw_thread_loop_unlock(impl_->loop);
        if (error) *error = "pw_context_new failed";
        stop();
        return false;
    }
    // The descriptor is duplicated: pw_context_connect_fd closes what it is
    // given, and the caller's copy has to outlive a reconnect.
    impl_->core = pw_context_connect_fd(impl_->context, ::dup(fd), nullptr, 0);
    if (impl_->core == nullptr) {
        pw_thread_loop_unlock(impl_->loop);
        if (error) *error = "pw_context_connect_fd failed - the portal's "
                            "remote is not usable";
        stop();
        return false;
    }

    pw_properties *props = pw_properties_new(
        PW_KEY_MEDIA_TYPE, "Video",
        PW_KEY_MEDIA_CATEGORY, "Capture",
        PW_KEY_MEDIA_ROLE, "Screen",
        nullptr);
    impl_->stream = pw_stream_new(impl_->core, "NeuralScreen capture", props);
    if (impl_->stream == nullptr) {
        pw_thread_loop_unlock(impl_->loop);
        if (error) *error = "pw_stream_new failed";
        stop();
        return false;
    }
    pw_stream_add_listener(impl_->stream, &impl_->stream_listener,
                           &kStreamEvents, impl_);

    // Offer every format we can import, best first. The compositor picks;
    // saying BGRx first costs nothing and gets the common case with no
    // conversion anywhere.
    uint8_t buffer[2048];
    spa_pod_builder builder = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    spa_rectangle size_default = SPA_RECTANGLE(1920, 1080);
    spa_rectangle size_min = SPA_RECTANGLE(1, 1);
    spa_rectangle size_max = SPA_RECTANGLE(8192, 8192);
    spa_fraction rate_default = SPA_FRACTION(60, 1);
    spa_fraction rate_min = SPA_FRACTION(0, 1);
    spa_fraction rate_max = SPA_FRACTION(360, 1);

    const spa_pod *params[1];
    params[0] = static_cast<const spa_pod *>(spa_pod_builder_add_object(
        &builder,
        SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
        SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format,
        SPA_POD_CHOICE_ENUM_Id(7, SPA_VIDEO_FORMAT_BGRx,
                               SPA_VIDEO_FORMAT_BGRx, SPA_VIDEO_FORMAT_BGRA,
                               SPA_VIDEO_FORMAT_RGBx, SPA_VIDEO_FORMAT_RGBA,
                               SPA_VIDEO_FORMAT_xBGR_210LE,
                               SPA_VIDEO_FORMAT_ABGR_210LE),
        SPA_FORMAT_VIDEO_size,
        SPA_POD_CHOICE_RANGE_Rectangle(&size_default, &size_min, &size_max),
        SPA_FORMAT_VIDEO_framerate,
        SPA_POD_CHOICE_RANGE_Fraction(&rate_default, &rate_min, &rate_max)));

    const int rc = pw_stream_connect(
        impl_->stream, PW_DIRECTION_INPUT, node,
        static_cast<pw_stream_flags>(PW_STREAM_FLAG_AUTOCONNECT
                                     | PW_STREAM_FLAG_MAP_BUFFERS),
        params, 1);
    pw_thread_loop_unlock(impl_->loop);
    if (rc < 0) {
        if (error) *error = "pw_stream_connect failed";
        stop();
        return false;
    }
    if (pw_thread_loop_start(impl_->loop) < 0) {
        if (error) *error = "pw_thread_loop_start failed";
        stop();
        return false;
    }
    running_ = true;
    log("[pw] reading node %u", node);
    return true;
}

void PwCapture::stop()
{
    running_ = false;
    if (impl_ == nullptr) return;
    if (impl_->loop != nullptr) pw_thread_loop_stop(impl_->loop);
    if (impl_->stream != nullptr) {
        pw_stream_destroy(impl_->stream);
        impl_->stream = nullptr;
    }
    if (impl_->core != nullptr) {
        pw_core_disconnect(impl_->core);
        impl_->core = nullptr;
    }
    if (impl_->context != nullptr) {
        pw_context_destroy(impl_->context);
        impl_->context = nullptr;
    }
    if (impl_->loop != nullptr) {
        pw_thread_loop_destroy(impl_->loop);
        impl_->loop = nullptr;
    }
    delete impl_;
    impl_ = nullptr;
}

void PwCapture::size(uint32_t *w, uint32_t *h) const
{
    uint32_t width = 0, height = 0;
    if (impl_ != nullptr) {
        std::lock_guard<std::mutex> guard(impl_->mutex);
        width = impl_->width;
        height = impl_->height;
    }
    if (w) *w = width;
    if (h) *h = height;
}

void PwCapture::note_frame() { ++frames_; }

}  // namespace ns
