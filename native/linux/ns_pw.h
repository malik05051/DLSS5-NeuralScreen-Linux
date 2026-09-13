// The capture source: one PipeWire stream, granted by the portal.
//
// This is what replaced Desktop Duplication and Windows Graphics Capture,
// and it replaced both of them at once - which is the nice part. On Windows
// those were two different APIs producing two different kinds of texture,
// and the host carried both paths; here a monitor and a window are the same
// thing, a node, and the only difference is which one the portal granted.
//
// The host never opens a stream by itself. Python negotiates with the
// portal, inherits the PipeWire remote as a file descriptor and passes the
// node id down; this class connects to that fd and reads that node. A
// worker that could open its own capture would be a worker that could
// capture without the user having agreed, which is the thing Wayland is
// built not to allow.
//
// Buffers arrive one of two ways and both are handled:
//
//   * **dmabuf** - the compositor's own buffer, imported into Vulkan with
//     no copy. The normal path on any modern stack.
//   * **memfd or a plain pointer** - shared memory, which happens on
//     software rendering and on some cross-GPU setups. Copied into a
//     staging buffer, which costs the same as the Windows fallback did.

#pragma once

#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <vector>

#include "ns_vk.h"

namespace ns {

//: One frame as PipeWire handed it over. Valid only inside the callback.
struct PwFrame {
    uint32_t width = 0, height = 0;
    //: The DRM fourcc, translated to the Vulkan format we will import as.
    VkFormat format = VK_FORMAT_UNDEFINED;
    bool is_dmabuf = false;

    // dmabuf
    uint64_t modifier = 0;
    std::vector<DmabufPlane> planes;

    // shared memory
    const uint8_t *pixels = nullptr;
    uint32_t stride = 0;
    size_t size = 0;
};

class PwCapture {
public:
    ~PwCapture();

    //: Called on PipeWire's own thread, once per frame. Keep it short.
    using Sink = std::function<void(const PwFrame &)>;

    // `fd` is the inherited PipeWire remote; it is duplicated, so the caller
    // keeps ownership of the original. `node` is the portal's node id.
    bool start(int fd, uint32_t node, Sink sink, std::string *error);
    void stop();

    bool running() const { return running_; }
    //: The negotiated size, once the format has been agreed. (0, 0) before.
    void size(uint32_t *w, uint32_t *h) const;
    //: How many frames have arrived. The watchdog on the Python side asks
    //: the same question from the other end; this one answers it locally.
    uint64_t frames() const { return frames_; }

    struct Impl;          // public so the C callbacks can see it
    //: Counted from the PipeWire thread, read from the loop. Public for the
    //: same reason - the process callback is a free function.
    void note_frame();

private:
    Impl *impl_ = nullptr;
    bool running_ = false;
    uint64_t frames_ = 0;
};

//: A DRM fourcc as PipeWire reports it -> the Vulkan format to import it as.
//: VK_FORMAT_UNDEFINED when we would not know what to do with it, which is
//: how the format negotiation decides what to ask for.
VkFormat vk_format_for_spa(uint32_t spa_video_format);

}  // namespace ns
