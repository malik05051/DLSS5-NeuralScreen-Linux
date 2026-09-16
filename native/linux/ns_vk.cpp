#include "ns_vk.h"

#include <cstdio>
#include <cstring>
#include <dlfcn.h>
#include <unistd.h>

#include <vulkan/vulkan.h>

#include <nvsdk_ngx.h>
#include <nvsdk_ngx_vk.h>
#include <nvsdk_ngx_helpers_vk.h>

namespace ns {

extern void log(const char *fmt, ...);   // host.cpp owns the log file

namespace {

// NGX's own feature id for Neural Rendering. It is not in the public enum -
// the SDK calls slot 18 "Reserved18" - and the Windows host uses exactly the
// same constant. The name here says what it is rather than what the header
// calls it.
const NVSDK_NGX_Feature kFeatureNeuralRendering =
    static_cast<NVSDK_NGX_Feature>(18);

// The application id NGX is initialised with. Any non-zero value works;
// this one is the Windows build's, kept so a driver-side profile made for
// it applies here too.
const unsigned long long kAppId = 0x4E535246ull;

VkImageAspectFlags aspect_of(VkFormat) { return VK_IMAGE_ASPECT_COLOR_BIT; }

// --- NGX, resolved at load time -----------------------------------------
//
// The Windows host did this too, and for the better of the two reasons: it
// is what makes the runtime swappable, so a different NR build can be
// dropped in and pointed at with NS_NR_DLL without rebuilding anything.
// Here it is also what makes the host buildable at all - the NGX SDK's
// static library is not part of this source tree, and requiring it would
// mean nobody could compile the program without an NVIDIA developer
// account.
//
// The names are the SDK's own, so a library that exports them is by
// definition the right one; a library that does not is reported by name
// rather than crashing on the first call.

struct NgxApi {
    NVSDK_NGX_Result (*Init)(unsigned long long, const wchar_t *, VkInstance,
                             VkPhysicalDevice, VkDevice,
                             NVSDK_NGX_Version) = nullptr;
    NVSDK_NGX_Result (*Shutdown1)(VkDevice) = nullptr;
    NVSDK_NGX_Result (*Shutdown)() = nullptr;
    NVSDK_NGX_Result (*AllocateParameters)(NVSDK_NGX_Parameter **) = nullptr;
    NVSDK_NGX_Result (*DestroyParameters)(NVSDK_NGX_Parameter *) = nullptr;
    NVSDK_NGX_Result (*GetCapabilityParameters)(NVSDK_NGX_Parameter **) = nullptr;
    NVSDK_NGX_Result (*CreateFeature)(VkCommandBuffer, NVSDK_NGX_Feature,
                                      const NVSDK_NGX_Parameter *,
                                      NVSDK_NGX_Handle **) = nullptr;
    NVSDK_NGX_Result (*ReleaseFeature)(NVSDK_NGX_Handle *) = nullptr;
    NVSDK_NGX_Result (*EvaluateFeature)(VkCommandBuffer,
                                        const NVSDK_NGX_Handle *,
                                        const NVSDK_NGX_Parameter *,
                                        PFN_NVSDK_NGX_ProgressCallback) = nullptr;
    void *handle = nullptr;
    std::string library;

    bool complete() const
    {
        return Init && Shutdown1 && AllocateParameters && DestroyParameters
               && CreateFeature && ReleaseFeature && EvaluateFeature;
    }
};

NgxApi g_ngx;

template <typename Fn>
void bind(Fn &slot, void *handle, const char *name)
{
    slot = reinterpret_cast<Fn>(dlsym(handle, name));
}

bool load_ngx(std::string *error)
{
    if (g_ngx.handle != nullptr) return g_ngx.complete();

    // In order: an explicit override, the SDK's own shared build, and the
    // names the driver package installs.
    std::vector<std::string> candidates;
    if (const char *forced = getenv("NS_NGX_LIB")) candidates.emplace_back(forced);
    candidates.insert(candidates.end(), {
        "libnvsdk_ngx.so", "libnvidia-ngx.so.1", "libnvidia-ngx.so",
        "/usr/lib/x86_64-linux-gnu/libnvidia-ngx.so.1",
        "/usr/lib64/libnvidia-ngx.so.1",
    });
    std::string tried;
    for (const std::string &name : candidates) {
        void *handle = dlopen(name.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (handle == nullptr) {
            if (!tried.empty()) tried += ", ";
            tried += name;
            continue;
        }
        g_ngx.handle = handle;
        g_ngx.library = name;
        bind(g_ngx.Init, handle, "NVSDK_NGX_VULKAN_Init");
        bind(g_ngx.Shutdown1, handle, "NVSDK_NGX_VULKAN_Shutdown1");
        bind(g_ngx.Shutdown, handle, "NVSDK_NGX_VULKAN_Shutdown");
        bind(g_ngx.AllocateParameters, handle,
             "NVSDK_NGX_VULKAN_AllocateParameters");
        bind(g_ngx.DestroyParameters, handle,
             "NVSDK_NGX_VULKAN_DestroyParameters");
        bind(g_ngx.GetCapabilityParameters, handle,
             "NVSDK_NGX_VULKAN_GetCapabilityParameters");
        bind(g_ngx.CreateFeature, handle, "NVSDK_NGX_VULKAN_CreateFeature");
        bind(g_ngx.ReleaseFeature, handle, "NVSDK_NGX_VULKAN_ReleaseFeature");
        bind(g_ngx.EvaluateFeature, handle,
             "NVSDK_NGX_VULKAN_EvaluateFeature");
        if (g_ngx.complete()) {
            log("[ngx] runtime: %s", name.c_str());
            return true;
        }
        log("[ngx] %s loaded but does not export the Vulkan entry points",
            name.c_str());
        dlclose(handle);
        g_ngx.handle = nullptr;
    }
    if (error) {
        *error = "no NGX runtime could be loaded (tried " + tried
                 + ") - install the NVIDIA driver, or point NS_NGX_LIB at it";
    }
    return false;
}

}  // namespace

const char *ngx_result_name(uint32_t result)
{
    switch (result) {
    case 0x1: return "Success";
    case 0xBAD00001: return "FAIL_FeatureNotSupported";
    case 0xBAD00002: return "FAIL_PlatformError";
    case 0xBAD00003: return "FAIL_FeatureAlreadyExists";
    case 0xBAD00004: return "FAIL_FeatureNotFound";
    case 0xBAD00005: return "FAIL_InvalidParameter";
    case 0xBAD00006: return "FAIL_ScratchBufferTooSmall";
    case 0xBAD00007: return "FAIL_NotInitialized";
    case 0xBAD00008: return "FAIL_UnsupportedInputFormat";
    case 0xBAD00009: return "FAIL_RWFlagMissing";
    case 0xBAD0000A: return "FAIL_MissingInput";
    case 0xBAD0000B: return "FAIL_UnableToInitializeFeature";
    case 0xBAD0000C: return "FAIL_OutOfDate";
    case 0xBAD0000D: return "FAIL_OutOfGPUMemory";
    case 0xBAD0000E: return "FAIL_UnsupportedFormat";
    case 0xBAD0000F: return "FAIL_UnableToWriteToAppDataPath";
    case 0xBAD00010: return "FAIL_UnsupportedParameter";
    case 0xBAD00011: return "FAIL_Denied";
    case 0xBAD00012: return "FAIL_NotImplemented";
    default: return "unknown";
    }
}

// ---------------------------------------------------------------------------
// Device
// ---------------------------------------------------------------------------

Device::~Device() { destroy(); }

bool Device::create(int gpu_index, std::string *error)
{
    VkApplicationInfo app{};
    app.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    app.pApplicationName = "NeuralScreen";
    app.applicationVersion = 1;
    app.pEngineName = "NeuralScreen";
    app.engineVersion = 1;
    // 1.1 is the floor: external memory and the fd-based import moved into
    // core there, and every driver that has NGX has far more than 1.1.
    app.apiVersion = VK_API_VERSION_1_2;

    const char *instance_exts[] = {
        VK_KHR_EXTERNAL_MEMORY_CAPABILITIES_EXTENSION_NAME,
        VK_KHR_GET_PHYSICAL_DEVICE_PROPERTIES_2_EXTENSION_NAME,
    };
    VkInstanceCreateInfo ici{};
    ici.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    ici.pApplicationInfo = &app;
    ici.enabledExtensionCount = 2;
    ici.ppEnabledExtensionNames = instance_exts;
    if (vkCreateInstance(&ici, nullptr, &instance_) != VK_SUCCESS) {
        // The extensions are promoted to core in 1.1, so a driver that
        // refuses them by name is still usable - try again without.
        ici.enabledExtensionCount = 0;
        ici.ppEnabledExtensionNames = nullptr;
        if (vkCreateInstance(&ici, nullptr, &instance_) != VK_SUCCESS) {
            if (error) *error = "vkCreateInstance failed - no usable Vulkan "
                                "driver (is the NVIDIA driver installed?)";
            return false;
        }
    }
    if (!pick_physical(gpu_index, error)) return false;

    vkGetPhysicalDeviceMemoryProperties(physical_, &mem_props_);

    uint32_t family_count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(physical_, &family_count, nullptr);
    std::vector<VkQueueFamilyProperties> families(family_count);
    vkGetPhysicalDeviceQueueFamilyProperties(physical_, &family_count,
                                             families.data());
    bool found = false;
    for (uint32_t i = 0; i < family_count; ++i) {
        // Graphics implies transfer and compute on every NVIDIA device, and
        // NGX submits compute into whatever command buffer it is handed.
        if (families[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) {
            queue_family_ = i;
            found = true;
            break;
        }
    }
    if (!found) {
        if (error) *error = "the device has no graphics queue family";
        return false;
    }

    // What we would like. Each one is checked against the device rather than
    // demanded: a missing dmabuf import means the capture falls back to a
    // memfd copy, which is slower and works.
    uint32_t ext_count = 0;
    vkEnumerateDeviceExtensionProperties(physical_, nullptr, &ext_count,
                                         nullptr);
    std::vector<VkExtensionProperties> available(ext_count);
    vkEnumerateDeviceExtensionProperties(physical_, nullptr, &ext_count,
                                         available.data());
    auto have = [&](const char *name) {
        for (const auto &e : available)
            if (std::strcmp(e.extensionName, name) == 0) return true;
        return false;
    };
    std::vector<const char *> device_exts;
    for (const char *name : {VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME,
                             VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME,
                             VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME,
                             VK_EXT_QUEUE_FAMILY_FOREIGN_EXTENSION_NAME,
                             VK_KHR_PUSH_DESCRIPTOR_EXTENSION_NAME}) {
        if (have(name)) device_exts.push_back(name);
    }
    has_dmabuf_ = have(VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME)
                  && have(VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME);

    const float priority = 1.0f;
    VkDeviceQueueCreateInfo qci{};
    qci.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
    qci.queueFamilyIndex = queue_family_;
    qci.queueCount = 1;
    qci.pQueuePriorities = &priority;

    VkPhysicalDeviceFeatures features{};
    vkGetPhysicalDeviceFeatures(physical_, &features);

    VkDeviceCreateInfo dci{};
    dci.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    dci.queueCreateInfoCount = 1;
    dci.pQueueCreateInfos = &qci;
    dci.enabledExtensionCount = static_cast<uint32_t>(device_exts.size());
    dci.ppEnabledExtensionNames = device_exts.data();
    dci.pEnabledFeatures = &features;
    if (vkCreateDevice(physical_, &dci, nullptr, &device_) != VK_SUCCESS) {
        if (error) *error = "vkCreateDevice failed";
        return false;
    }
    vkGetDeviceQueue(device_, queue_family_, 0, &queue_);

    VkCommandPoolCreateInfo pci{};
    pci.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pci.queueFamilyIndex = queue_family_;
    if (vkCreateCommandPool(device_, &pci, nullptr, &pool_) != VK_SUCCESS) {
        if (error) *error = "vkCreateCommandPool failed";
        return false;
    }

    get_memory_fd_ = reinterpret_cast<PFN_vkGetMemoryFdKHR>(
        vkGetDeviceProcAddr(device_, "vkGetMemoryFdKHR"));

    log("[vk] device: %s (dmabuf import: %s)", name_.c_str(),
        has_dmabuf_ ? "yes" : "no - frames are copied through memory");
    return true;
}

bool Device::pick_physical(int gpu_index, std::string *error)
{
    uint32_t count = 0;
    vkEnumeratePhysicalDevices(instance_, &count, nullptr);
    if (count == 0) {
        if (error) *error = "no Vulkan device at all";
        return false;
    }
    std::vector<VkPhysicalDevice> devices(count);
    vkEnumeratePhysicalDevices(instance_, &count, devices.data());

    // NVIDIA only, in the driver's own order - which is the order NVML
    // reports and therefore the order the menu shows, so NS_GPU means the
    // same number everywhere.
    std::vector<VkPhysicalDevice> nvidia;
    std::vector<std::string> names;
    for (VkPhysicalDevice candidate : devices) {
        VkPhysicalDeviceProperties props{};
        vkGetPhysicalDeviceProperties(candidate, &props);
        if (props.vendorID != 0x10DE) continue;
        nvidia.push_back(candidate);
        names.emplace_back(props.deviceName);
        log("[vk] device %zu: %s", nvidia.size() - 1, props.deviceName);
    }
    if (nvidia.empty()) {
        if (error) *error = "no NVIDIA device - the neural renderer cannot "
                            "run anywhere else";
        return false;
    }
    size_t chosen = 0;
    if (gpu_index >= 0 && static_cast<size_t>(gpu_index) < nvidia.size()) {
        chosen = static_cast<size_t>(gpu_index);
    } else if (gpu_index >= 0) {
        log("[vk] NS_GPU=%d but there are only %zu NVIDIA devices - using 0",
            gpu_index, nvidia.size());
    }
    physical_ = nvidia[chosen];
    name_ = names[chosen];
    return true;
}

void Device::destroy()
{
    if (device_ != VK_NULL_HANDLE) {
        vkDeviceWaitIdle(device_);
        if (pool_ != VK_NULL_HANDLE) {
            vkDestroyCommandPool(device_, pool_, nullptr);
            pool_ = VK_NULL_HANDLE;
        }
        vkDestroyDevice(device_, nullptr);
        device_ = VK_NULL_HANDLE;
    }
    if (instance_ != VK_NULL_HANDLE) {
        vkDestroyInstance(instance_, nullptr);
        instance_ = VK_NULL_HANDLE;
    }
}

uint32_t Device::memory_type(uint32_t bits, VkMemoryPropertyFlags want) const
{
    for (uint32_t i = 0; i < mem_props_.memoryTypeCount; ++i) {
        if ((bits & (1u << i))
            && (mem_props_.memoryTypes[i].propertyFlags & want) == want) {
            return i;
        }
    }
    return UINT32_MAX;
}

bool Device::make_image(Image &out, uint32_t w, uint32_t h, VkFormat format,
                        VkImageUsageFlags usage)
{
    destroy_image(out);
    VkImageCreateInfo ici{};
    ici.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    ici.imageType = VK_IMAGE_TYPE_2D;
    ici.format = format;
    ici.extent = {w, h, 1};
    ici.mipLevels = 1;
    ici.arrayLayers = 1;
    ici.samples = VK_SAMPLE_COUNT_1_BIT;
    ici.tiling = VK_IMAGE_TILING_OPTIMAL;
    // STORAGE is not optional: NGX writes the result through an image store,
    // and a resource without it comes back as FAIL_RWFlagMissing - which is
    // the Vulkan spelling of the same mistake the D3D12 host could make with
    // a missing UAV flag.
    ici.usage = usage | VK_IMAGE_USAGE_STORAGE_BIT
                | VK_IMAGE_USAGE_SAMPLED_BIT
                | VK_IMAGE_USAGE_TRANSFER_SRC_BIT
                | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    ici.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    ici.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    if (vkCreateImage(device_, &ici, nullptr, &out.image) != VK_SUCCESS)
        return false;

    VkMemoryRequirements req{};
    vkGetImageMemoryRequirements(device_, out.image, &req);
    VkMemoryAllocateInfo mai{};
    mai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    mai.allocationSize = req.size;
    mai.memoryTypeIndex = memory_type(req.memoryTypeBits,
                                      VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    if (mai.memoryTypeIndex == UINT32_MAX
        || vkAllocateMemory(device_, &mai, nullptr, &out.memory) != VK_SUCCESS) {
        vkDestroyImage(device_, out.image, nullptr);
        out.image = VK_NULL_HANDLE;
        return false;
    }
    vkBindImageMemory(device_, out.image, out.memory, 0);

    VkImageViewCreateInfo vci{};
    vci.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
    vci.image = out.image;
    vci.viewType = VK_IMAGE_VIEW_TYPE_2D;
    vci.format = format;
    vci.subresourceRange = {aspect_of(format), 0, 1, 0, 1};
    vkCreateImageView(device_, &vci, nullptr, &out.view);

    out.format = format;
    out.width = w;
    out.height = h;
    out.layout = VK_IMAGE_LAYOUT_UNDEFINED;
    out.imported = false;
    return true;
}

bool Device::import_dmabuf(Image &out, uint32_t w, uint32_t h, VkFormat format,
                           uint64_t modifier,
                           const std::vector<DmabufPlane> &planes)
{
    destroy_image(out);
    if (!has_dmabuf_ || planes.empty()) return false;

    std::vector<VkSubresourceLayout> layouts(planes.size());
    for (size_t i = 0; i < planes.size(); ++i) {
        layouts[i] = {};
        layouts[i].offset = planes[i].offset;
        layouts[i].rowPitch = planes[i].stride;
    }

    VkImageDrmFormatModifierExplicitCreateInfoEXT mod{};
    mod.sType =
        VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT;
    mod.drmFormatModifier = modifier;
    mod.drmFormatModifierPlaneCount = static_cast<uint32_t>(layouts.size());
    mod.pPlaneLayouts = layouts.data();

    VkExternalMemoryImageCreateInfo ext{};
    ext.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO;
    ext.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
    ext.pNext = &mod;

    VkImageCreateInfo ici{};
    ici.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    ici.pNext = &ext;
    ici.imageType = VK_IMAGE_TYPE_2D;
    ici.format = format;
    ici.extent = {w, h, 1};
    ici.mipLevels = 1;
    ici.arrayLayers = 1;
    ici.samples = VK_SAMPLE_COUNT_1_BIT;
    ici.tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT;
    // Sampled and transfer only: the compositor's buffer is somebody else's
    // memory and is read, never written.
    ici.usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    ici.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    ici.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    if (vkCreateImage(device_, &ici, nullptr, &out.image) != VK_SUCCESS)
        return false;

    VkMemoryRequirements req{};
    vkGetImageMemoryRequirements(device_, out.image, &req);

    VkImportMemoryFdInfoKHR import{};
    import.sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR;
    import.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
    // Vulkan takes ownership of the descriptor and closes it with the
    // memory, so it must be a dup of PipeWire's - the caller does that.
    import.fd = planes[0].fd;

    VkMemoryDedicatedAllocateInfo dedicated{};
    dedicated.sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO;
    dedicated.image = out.image;
    import.pNext = &dedicated;

    VkMemoryAllocateInfo mai{};
    mai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    mai.pNext = &import;
    mai.allocationSize = req.size;
    mai.memoryTypeIndex = memory_type(req.memoryTypeBits, 0);
    if (mai.memoryTypeIndex == UINT32_MAX
        || vkAllocateMemory(device_, &mai, nullptr, &out.memory) != VK_SUCCESS) {
        vkDestroyImage(device_, out.image, nullptr);
        out.image = VK_NULL_HANDLE;
        return false;
    }
    vkBindImageMemory(device_, out.image, out.memory, 0);

    VkImageViewCreateInfo vci{};
    vci.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
    vci.image = out.image;
    vci.viewType = VK_IMAGE_VIEW_TYPE_2D;
    vci.format = format;
    vci.subresourceRange = {aspect_of(format), 0, 1, 0, 1};
    vkCreateImageView(device_, &vci, nullptr, &out.view);

    out.format = format;
    out.width = w;
    out.height = h;
    out.layout = VK_IMAGE_LAYOUT_UNDEFINED;
    out.imported = true;
    return true;
}

void Device::destroy_image(Image &img)
{
    if (device_ == VK_NULL_HANDLE) return;
    if (img.view != VK_NULL_HANDLE) {
        vkDestroyImageView(device_, img.view, nullptr);
        img.view = VK_NULL_HANDLE;
    }
    if (img.image != VK_NULL_HANDLE) {
        vkDestroyImage(device_, img.image, nullptr);
        img.image = VK_NULL_HANDLE;
    }
    if (img.memory != VK_NULL_HANDLE) {
        vkFreeMemory(device_, img.memory, nullptr);
        img.memory = VK_NULL_HANDLE;
    }
    img.layout = VK_IMAGE_LAYOUT_UNDEFINED;
    img.width = img.height = 0;
    img.imported = false;
}

bool Device::make_buffer(Buffer &out, VkDeviceSize size,
                         VkBufferUsageFlags usage, bool host_visible)
{
    destroy_buffer(out);
    VkBufferCreateInfo bci{};
    bci.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bci.size = size;
    bci.usage = usage;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (vkCreateBuffer(device_, &bci, nullptr, &out.buffer) != VK_SUCCESS)
        return false;

    VkMemoryRequirements req{};
    vkGetBufferMemoryRequirements(device_, out.buffer, &req);
    const VkMemoryPropertyFlags want =
        host_visible ? (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                        | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)
                     : VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT;
    VkMemoryAllocateInfo mai{};
    mai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    mai.allocationSize = req.size;
    mai.memoryTypeIndex = memory_type(req.memoryTypeBits, want);
    if (mai.memoryTypeIndex == UINT32_MAX
        || vkAllocateMemory(device_, &mai, nullptr, &out.memory) != VK_SUCCESS) {
        vkDestroyBuffer(device_, out.buffer, nullptr);
        out.buffer = VK_NULL_HANDLE;
        return false;
    }
    vkBindBufferMemory(device_, out.buffer, out.memory, 0);
    out.size = size;
    if (host_visible)
        vkMapMemory(device_, out.memory, 0, size, 0, &out.mapped);
    return true;
}

void Device::destroy_buffer(Buffer &buf)
{
    if (device_ == VK_NULL_HANDLE) return;
    if (buf.mapped != nullptr) {
        vkUnmapMemory(device_, buf.memory);
        buf.mapped = nullptr;
    }
    if (buf.buffer != VK_NULL_HANDLE) {
        vkDestroyBuffer(device_, buf.buffer, nullptr);
        buf.buffer = VK_NULL_HANDLE;
    }
    if (buf.memory != VK_NULL_HANDLE) {
        vkFreeMemory(device_, buf.memory, nullptr);
        buf.memory = VK_NULL_HANDLE;
    }
    buf.size = 0;
}

VkCommandBuffer Device::begin()
{
    VkCommandBufferAllocateInfo ai{};
    ai.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    ai.commandPool = pool_;
    ai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    ai.commandBufferCount = 1;
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    if (vkAllocateCommandBuffers(device_, &ai, &cmd) != VK_SUCCESS)
        return VK_NULL_HANDLE;
    VkCommandBufferBeginInfo bi{};
    bi.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (vkBeginCommandBuffer(cmd, &bi) != VK_SUCCESS) {
        vkFreeCommandBuffers(device_, pool_, 1, &cmd);
        return VK_NULL_HANDLE;
    }
    return cmd;
}

bool Device::submit_and_wait(VkCommandBuffer cmd)
{
    if (cmd == VK_NULL_HANDLE) return false;
    bool ok = vkEndCommandBuffer(cmd) == VK_SUCCESS;
    if (ok) {
        VkFenceCreateInfo fci{};
        fci.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
        VkFence fence = VK_NULL_HANDLE;
        vkCreateFence(device_, &fci, nullptr, &fence);
        VkSubmitInfo si{};
        si.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
        si.commandBufferCount = 1;
        si.pCommandBuffers = &cmd;
        ok = vkQueueSubmit(queue_, 1, &si, fence) == VK_SUCCESS;
        if (ok) {
            // Two seconds: an evaluation that has not finished by then is a
            // hung GPU, and the watchdog on the Python side is about to
            // notice anyway. Waiting forever here is how the worker becomes
            // a process that cannot be killed cleanly.
            ok = vkWaitForFences(device_, 1, &fence, VK_TRUE,
                                 2ull * 1000 * 1000 * 1000) == VK_SUCCESS;
        }
        vkDestroyFence(device_, fence, nullptr);
    }
    vkFreeCommandBuffers(device_, pool_, 1, &cmd);
    return ok;
}

void Device::barrier(VkCommandBuffer cmd, Image &img, VkImageLayout to)
{
    if (img.layout == to || !img.valid()) return;
    VkImageMemoryBarrier b{};
    b.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    b.oldLayout = img.layout;
    b.newLayout = to;
    b.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = img.image;
    b.subresourceRange = {aspect_of(img.format), 0, 1, 0, 1};
    // Broad masks on purpose: this runs a handful of times per frame, not
    // per draw, and a correct pipeline barrier is worth more here than a
    // tight one.
    b.srcAccessMask = VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_MEMORY_READ_BIT;
    b.dstAccessMask = VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_MEMORY_READ_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
                         VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, 0, 0, nullptr,
                         0, nullptr, 1, &b);
    img.layout = to;
}

void Device::copy_buffer_to_image(VkCommandBuffer cmd, const Buffer &src,
                                  Image &dst)
{
    barrier(cmd, dst, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
    VkBufferImageCopy region{};
    region.imageSubresource = {aspect_of(dst.format), 0, 0, 1};
    region.imageExtent = {dst.width, dst.height, 1};
    vkCmdCopyBufferToImage(cmd, src.buffer, dst.image,
                           VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);
}

void Device::copy_image_to_buffer(VkCommandBuffer cmd, Image &src,
                                  const Buffer &dst)
{
    barrier(cmd, src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
    VkBufferImageCopy region{};
    region.imageSubresource = {aspect_of(src.format), 0, 0, 1};
    region.imageExtent = {src.width, src.height, 1};
    vkCmdCopyImageToBuffer(cmd, src.image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           dst.buffer, 1, &region);
}

void Device::blit(VkCommandBuffer cmd, Image &src, Image &dst, bool linear)
{
    barrier(cmd, src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
    barrier(cmd, dst, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
    VkImageBlit region{};
    region.srcSubresource = {aspect_of(src.format), 0, 0, 1};
    region.dstSubresource = {aspect_of(dst.format), 0, 0, 1};
    region.srcOffsets[1] = {static_cast<int32_t>(src.width),
                            static_cast<int32_t>(src.height), 1};
    region.dstOffsets[1] = {static_cast<int32_t>(dst.width),
                            static_cast<int32_t>(dst.height), 1};
    vkCmdBlitImage(cmd, src.image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                   dst.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region,
                   linear ? VK_FILTER_LINEAR : VK_FILTER_NEAREST);
}

// ---------------------------------------------------------------------------
// NGX
// ---------------------------------------------------------------------------

Ngx::~Ngx() { }

bool Ngx::init(Device &device, const std::string &snippet_dir,
               std::string *error)
{
    if (!load_ngx(error)) return false;
    // NGX takes the application data path as wide characters even here.
    std::wstring path(snippet_dir.begin(), snippet_dir.end());
    NVSDK_NGX_Result r = g_ngx.Init(
        kAppId, path.c_str(), device.instance(), device.physical(),
        device.handle(), NVSDK_NGX_Version_API);
    last_result_ = static_cast<uint32_t>(r);
    if (NVSDK_NGX_FAILED(r)) {
        if (error) {
            *error = "NVSDK_NGX_VULKAN_Init failed: "
                     + std::string(ngx_result_name(last_result_));
        }
        return false;
    }
    initialised_ = true;
    r = g_ngx.AllocateParameters(&params_);
    last_result_ = static_cast<uint32_t>(r);
    if (NVSDK_NGX_FAILED(r) || params_ == nullptr) {
        if (error) {
            *error = "NVSDK_NGX_VULKAN_AllocateParameters failed: "
                     + std::string(ngx_result_name(last_result_));
        }
        params_ = nullptr;
        return false;
    }
    log("[ngx] initialised on %s", device.name().c_str());
    return true;
}

void Ngx::shutdown(Device &device)
{
    if (params_ != nullptr) {
        g_ngx.DestroyParameters(params_);
        params_ = nullptr;
    }
    if (initialised_) {
        // Shutdown1 rather than Shutdown: the unversioned entry point is
        // compiled out of the current SDK headers, and the device-scoped one
        // is what it became. Passing the device is also the honest call -
        // NGX's state belongs to it.
        if (g_ngx.Shutdown) g_ngx.Shutdown(); else g_ngx.Shutdown1(device.handle());
        initialised_ = false;
    }
}

bool Ngx::create_feature(Device &device, uint32_t work_w, uint32_t work_h,
                         uint32_t full_w, uint32_t full_h)
{
    if (params_ == nullptr) return false;
    release_feature(device);

    // The same contract the Windows host sets, parameter for parameter. The
    // names belong to the feature, not to the graphics API, so nothing here
    // changes between D3D12 and Vulkan.
    const bool upscale = full_w > 0 && full_h > 0
                         && (full_w != work_w || full_h != work_h);
    const uint32_t io_w = upscale ? full_w : work_w;
    const uint32_t io_h = upscale ? full_h : work_h;
    const float ratio = upscale ? static_cast<float>(work_w)
                                      / static_cast<float>(full_w)
                                : 1.0f;

    params_->Reset();
    params_->Set("CreationNodeMask", 1u);
    params_->Set("VisibilityNodeMask", 1u);
    params_->Set("DLSSNR.Width", work_w);
    params_->Set("DLSSNR.Height", work_h);
    params_->Set("DLSSNR.InputWidth", io_w);
    params_->Set("DLSSNR.InputHeight", io_h);
    params_->Set("DLSSNR.OutputWidth", io_w);
    params_->Set("DLSSNR.OutputHeight", io_h);
    params_->Set("DLSSNR.Output.Width", io_w);
    params_->Set("DLSSNR.Output.Height", io_h);
    params_->Set("DLSSNR.Upscaling", upscale ? 1u : 0u);
    params_->Set("DLSSNR.Scale", ratio);
    params_->Set("DLSSNR.ScalingRatio", ratio);
    params_->Set("DLSS.Feature.Create.Flags", 0u);

    VkCommandBuffer cmd = device.begin();
    if (cmd == VK_NULL_HANDLE) return false;
    NVSDK_NGX_Result r = g_ngx.CreateFeature(
        cmd, kFeatureNeuralRendering, params_, &feature_);
    last_result_ = static_cast<uint32_t>(r);
    const bool submitted = device.submit_and_wait(cmd);
    if (NVSDK_NGX_FAILED(r) || feature_ == nullptr || !submitted) {
        log("[ngx] CreateFeature(%ux%u -> %ux%u) failed: %s (0x%08X)",
            io_w, io_h, work_w, work_h, ngx_result_name(last_result_),
            last_result_);
        feature_ = nullptr;
        return false;
    }
    log("[ngx] feature 18 created: work %ux%u, io %ux%u, upscaling %s",
        work_w, work_h, io_w, io_h, upscale ? "on" : "off");
    return true;
}

uint32_t Ngx::probe_feature(Device &device, uint32_t feature_id,
                            uint32_t width, uint32_t height)
{
    if (params_ == nullptr) return 0xBAD00000;
    release_feature(device);

    // A parameter set every feature we know of accepts as a starting point.
    // We are not after a usable feature here, only NGX's verdict: a missing
    // snippet answers before it ever reads the parameters, and a present
    // one complains about *them* rather than about itself.
    params_->Reset();
    params_->Set("CreationNodeMask", 1u);
    params_->Set("VisibilityNodeMask", 1u);
    params_->Set("Width", width);
    params_->Set("Height", height);
    params_->Set("OutWidth", width);
    params_->Set("OutHeight", height);
    params_->Set("PerfQualityValue", 0u);
    params_->Set("DLSS.Feature.Create.Flags", 0u);
    params_->Set("DLSSNR.Width", width);
    params_->Set("DLSSNR.Height", height);
    params_->Set("DLSSNR.InputWidth", width);
    params_->Set("DLSSNR.InputHeight", height);
    params_->Set("DLSSNR.OutputWidth", width);
    params_->Set("DLSSNR.OutputHeight", height);

    VkCommandBuffer cmd = device.begin();
    if (cmd == VK_NULL_HANDLE) return 0xBAD00000;
    NVSDK_NGX_Handle *handle = nullptr;
    NVSDK_NGX_Result r = g_ngx.CreateFeature(
        cmd, static_cast<NVSDK_NGX_Feature>(feature_id), params_, &handle);
    device.submit_and_wait(cmd);
    if (!NVSDK_NGX_FAILED(r) && handle != nullptr) {
        vkDeviceWaitIdle(device.handle());
        g_ngx.ReleaseFeature(handle);
    }
    return static_cast<uint32_t>(r);
}

bool Ngx::capability(const char *name, int *value)
{
    if (g_ngx.GetCapabilityParameters == nullptr) return false;
    NVSDK_NGX_Parameter *caps = nullptr;
    NVSDK_NGX_Result r = g_ngx.GetCapabilityParameters(&caps);
    if (NVSDK_NGX_FAILED(r) || caps == nullptr) return false;
    // Capability maps are owned by NGX and must not be destroyed by us.
    return !NVSDK_NGX_FAILED(caps->Get(name, value));
}

void Ngx::release_feature(Device &device)
{
    if (feature_ == nullptr) return;
    vkDeviceWaitIdle(device.handle());
    g_ngx.ReleaseFeature(feature_);
    feature_ = nullptr;
}

namespace {

// Wrap one of our images the way NGX wants to see it. The helper in
// nvsdk_ngx_helpers_vk.h builds this struct field by field; doing it here
// keeps the read/write flag next to the comment explaining why it matters.
NVSDK_NGX_Resource_VK wrap(Image &img, bool writable)
{
    return NVSDK_NGX_Create_ImageView_Resource_VK(
        img.view, img.image,
        VkImageSubresourceRange{VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1},
        img.format, img.width, img.height, writable);
}

}  // namespace

bool Ngx::evaluate(Device &device, VkCommandBuffer cmd, Image &color,
                   Image &output, Image &motion, const NrOptions &options,
                   bool reset)
{
    if (params_ == nullptr || feature_ == nullptr) return false;

    // GENERAL rather than the more specific layouts: NGX binds these as
    // storage images inside its own pipeline, and it is not told which
    // layout we would have preferred.
    device.barrier(cmd, color, VK_IMAGE_LAYOUT_GENERAL);
    device.barrier(cmd, output, VK_IMAGE_LAYOUT_GENERAL);
    device.barrier(cmd, motion, VK_IMAGE_LAYOUT_GENERAL);

    NVSDK_NGX_Resource_VK color_res = wrap(color, false);
    NVSDK_NGX_Resource_VK output_res = wrap(output, true);
    NVSDK_NGX_Resource_VK motion_res = wrap(motion, false);

    params_->Set("DLSSNR.Color", &color_res);
    params_->Set("DLSSNR.Output", &output_res);
    params_->Set("DLSSNR.MVec", &motion_res);
    params_->Set("DLSSNR.ColorSubrectBaseX", 0u);
    params_->Set("DLSSNR.ColorSubrectBaseY", 0u);
    params_->Set("DLSSNR.ColorSubrectWidth", color.width);
    params_->Set("DLSSNR.ColorSubrectHeight", color.height);
    params_->Set("DLSSNR.MVecSubrectBaseX", 0u);
    params_->Set("DLSSNR.MVecSubrectBaseY", 0u);
    params_->Set("DLSSNR.MVecSubrectWidth", motion.width);
    params_->Set("DLSSNR.MVecSubrectHeight", motion.height);
    params_->Set("DLSSNR.OutputSubrectBaseX", 0u);
    params_->Set("DLSSNR.OutputSubrectBaseY", 0u);
    params_->Set("DLSSNR.OutputSubrectWidth", output.width);
    params_->Set("DLSSNR.OutputSubrectHeight", output.height);
    params_->Set("DLSSNR.MVecScaleX", 1.0f);
    params_->Set("DLSSNR.MVecScaleY", 1.0f);
    params_->Set("DLSSNR.Enabled", 1u);
    params_->Set("DLSSNR.Reset", reset ? 1u : 0u);
    params_->Set("DLSSNR.Intensity", options.intensity);
    params_->Set("DLSSNR.LocalToneStrength", options.local_tone);
    params_->Set("DLSSNR.LocalStructureStrength", options.local_structure);
    params_->Set("DLSSNR.SkinStructureStrength", options.skin_structure);
    params_->Set("DLSSNR.UseAutoMask", options.auto_mask);
    params_->Set("DLSSNR.Style", options.style);
    params_->Set("DLSSNR.UICorrection", options.ui_correction);
    params_->Set("DLSS.Pre.Exposure", 1.0f);
    params_->Set("DLSS.Exposure.Scale", options.exposure);

    NVSDK_NGX_Result r =
        g_ngx.EvaluateFeature(cmd, feature_, params_, nullptr);
    last_result_ = static_cast<uint32_t>(r);
    return !NVSDK_NGX_FAILED(r);
}

}  // namespace ns
