// The Vulkan device the network runs on, and the NGX feature itself.
//
// The Windows host was a D3D12 program: it created a device, wrapped the
// captured frame in an ID3D12Resource and handed that to
// NVSDK_NGX_D3D12_EvaluateFeature. NGX has exactly the same shape on
// Vulkan - a device, a command buffer, resources - and the DLSSNR.*
// parameter names are the same strings, because they are the feature's
// contract rather than the API's.
//
// Two things Vulkan does that D3D12 did not have to:
//
//   * **Imports instead of shares.** A captured frame arrives from PipeWire
//     as a dmabuf, which is imported with VK_EXT_external_memory_dma_buf
//     rather than opened from an NT handle. That is the zero-copy path and
//     the reason the worker captures at all: the compositor's buffer
//     becomes a VkImage without a pixel moving.
//   * **The device must be the right one.** On Windows the capture and the
//     network had to be on the same adapter because a shared handle does
//     not cross adapters. Here the constraint is the same and the symptom
//     is different: a dmabuf from one GPU cannot be imported by another, so
//     the physical device is chosen to match the one the compositor is
//     rendering on unless NS_GPU says otherwise.

#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include <vulkan/vulkan.h>

struct NVSDK_NGX_Handle;
struct NVSDK_NGX_Parameter;

namespace ns {

//: One image the pipeline owns, or one imported from a dmabuf.
struct Image {
    VkImage image = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;
    VkImageView view = VK_NULL_HANDLE;
    VkFormat format = VK_FORMAT_UNDEFINED;
    uint32_t width = 0, height = 0;
    VkImageLayout layout = VK_IMAGE_LAYOUT_UNDEFINED;
    bool imported = false;   // the memory belongs to somebody else

    bool valid() const { return image != VK_NULL_HANDLE; }
};

//: A host-visible buffer, for getting pixels in and out.
struct Buffer {
    VkBuffer buffer = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;
    void *mapped = nullptr;
    VkDeviceSize size = 0;

    bool valid() const { return buffer != VK_NULL_HANDLE; }
};

//: A dmabuf plane as PipeWire describes one.
struct DmabufPlane {
    int fd = -1;
    uint32_t offset = 0;
    uint32_t stride = 0;
};

class Device {
public:
    ~Device();

    // `gpu_index` is NS_GPU: -1 means "pick the first NVIDIA device".
    bool create(int gpu_index, std::string *error);
    void destroy();

    VkDevice handle() const { return device_; }
    VkPhysicalDevice physical() const { return physical_; }
    VkInstance instance() const { return instance_; }
    VkQueue queue() const { return queue_; }
    uint32_t queue_family() const { return queue_family_; }
    const std::string &name() const { return name_; }
    bool has_dmabuf() const { return has_dmabuf_; }

    // -- resources ---------------------------------------------------------

    bool make_image(Image &out, uint32_t w, uint32_t h, VkFormat format,
                    VkImageUsageFlags usage);
    // Import a PipeWire dmabuf. Takes ownership of the plane descriptors:
    // Vulkan closes them when the memory is freed.
    bool import_dmabuf(Image &out, uint32_t w, uint32_t h, VkFormat format,
                       uint64_t modifier, const std::vector<DmabufPlane> &planes);
    void destroy_image(Image &img);

    bool make_buffer(Buffer &out, VkDeviceSize size, VkBufferUsageFlags usage,
                     bool host_visible);
    void destroy_buffer(Buffer &buf);

    // -- commands ----------------------------------------------------------

    VkCommandBuffer begin();             // VK_NULL_HANDLE on failure
    bool submit_and_wait(VkCommandBuffer cmd);

    void barrier(VkCommandBuffer cmd, Image &img, VkImageLayout to);
    void copy_buffer_to_image(VkCommandBuffer cmd, const Buffer &src,
                              Image &dst);
    void copy_image_to_buffer(VkCommandBuffer cmd, Image &src,
                              const Buffer &dst);
    void blit(VkCommandBuffer cmd, Image &src, Image &dst, bool linear);

private:
    bool pick_physical(int gpu_index, std::string *error);
    uint32_t memory_type(uint32_t bits, VkMemoryPropertyFlags want) const;

    VkInstance instance_ = VK_NULL_HANDLE;
    VkPhysicalDevice physical_ = VK_NULL_HANDLE;
    VkDevice device_ = VK_NULL_HANDLE;
    VkQueue queue_ = VK_NULL_HANDLE;
    VkCommandPool pool_ = VK_NULL_HANDLE;
    uint32_t queue_family_ = 0;
    VkPhysicalDeviceMemoryProperties mem_props_{};
    std::string name_;
    bool has_dmabuf_ = false;

    PFN_vkGetMemoryFdKHR get_memory_fd_ = nullptr;
};

// ---------------------------------------------------------------------------
// NGX
// ---------------------------------------------------------------------------

//: What the header and RNSZ carry, in one place.
struct NrOptions {
    uint32_t profile = 1, preset = 0, style = 1;
    uint32_t auto_mask = 0, ui_correction = 0;
    float intensity = 1.0f, local_tone = 0.5f;
    float local_structure = 1.0f, skin_structure = -1.0f;
    float exposure = 1.0f;        // adaptive exposure, from the dark-scene lift
};

class Ngx {
public:
    ~Ngx();

    // `snippet_dir` is where nvngx_dlssnr.so lives - NGX loads the feature's
    // own library out of the application path it is given.
    bool init(Device &device, const std::string &snippet_dir,
              std::string *error);
    void shutdown(Device &device);

    bool ready() const { return params_ != nullptr; }
    uint32_t last_result() const { return last_result_; }

    // work_w/work_h is the resolution the network runs at; full_w/full_h the
    // size of the frames coming in. Equal sizes mean no upscale, which is
    // the 1:1 path - the worker crashed in upscale mode when they matched.
    bool create_feature(Device &device, uint32_t work_w, uint32_t work_h,
                        uint32_t full_w, uint32_t full_h);
    void release_feature(Device &device);
    bool has_feature() const { return feature_ != nullptr; }

    // Diagnostics for --probe. probe_feature asks NGX to create an arbitrary
    // feature id with a generic parameter set and returns the raw result;
    // capability reads one integer out of NGX's capability map.
    uint32_t probe_feature(Device &device, uint32_t feature_id,
                           uint32_t width, uint32_t height);
    bool capability(const char *name, int *value);

    // One evaluation. `color` and `output` are the same size; `motion` is at
    // the work resolution.
    bool evaluate(Device &device, VkCommandBuffer cmd, Image &color,
                  Image &output, Image &motion, const NrOptions &options,
                  bool reset);

private:
    NVSDK_NGX_Parameter *params_ = nullptr;
    NVSDK_NGX_Handle *feature_ = nullptr;
    uint32_t last_result_ = 0;
    bool initialised_ = false;
};

//: The name of the NGX result code, for the log. NGX numbers are opaque and
//: a user pasting "0xBAD00005" into an issue is not helping anybody.
const char *ngx_result_name(uint32_t result);

}  // namespace ns
