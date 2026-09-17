// nvngx.dll_nr.exe - the neural renderer, on the Windows side of Proton.
//
// NVIDIA ships DLSSNR only as a D3D12 DLL, so this is the one process in
// the program that is not Linux code: it owns a D3D12 device through vkd3d-
// proton, loads nvngx_dlssnr.dll the way the Windows build did, and runs
// feature 18 on frames the Linux host puts in a shared mapping. It knows
// nothing about capture, the overlay or Python - see ns_nr_wire.h for the
// whole of what it knows.
//
//   nvngx.dll_nr.exe <unix path of the mapping> [unix path of nvngx_dlssnr.dll]
//
// Without the second argument the runtime is loaded from beside this exe.
//
// The file name carries "nvngx.dll" because the runtime refuses calls from
// a module whose path lacks that substring. NVSDK_NGX_Parameter is an MSVC
// C++ interface; its overloaded Set() methods sit in the vtable in the
// reverse of declaration order, which is not what GCC assumes, so the
// vtable is addressed by MSVC index below.
#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <fcntl.h>
#include <io.h>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <string>
#include "nvsdk_ngx.h"
#include "ns_nr_wire.h"

#define LOG(...) do { fprintf(stderr, "[nr] " __VA_ARGS__); fputc('\n', stderr); fflush(stderr); } while (0)

namespace {

using PFN_Init = NVSDK_NGX_Result (NVSDK_CONV *)(unsigned long long, const wchar_t *, ID3D12Device *,
                                                 const NVSDK_NGX_FeatureCommonInfo *, NVSDK_NGX_Version);
using PFN_Alloc = NVSDK_NGX_Result (NVSDK_CONV *)(NVSDK_NGX_Parameter **);
using PFN_InitExt = NVSDK_NGX_Result (NVSDK_CONV *)(unsigned long long, const wchar_t *, ID3D12Device *,
                                                    NVSDK_NGX_Version, const NVSDK_NGX_Parameter *);
using PFN_Create = NVSDK_NGX_Result (NVSDK_CONV *)(ID3D12GraphicsCommandList *, NVSDK_NGX_Feature,
                                                   const NVSDK_NGX_Parameter *, NVSDK_NGX_Handle **);
using PFN_Eval = NVSDK_NGX_Result (NVSDK_CONV *)(ID3D12GraphicsCommandList *, const NVSDK_NGX_Handle *,
                                                 const NVSDK_NGX_Parameter *, PFN_NVSDK_NGX_ProgressCallback);
using PFN_Release = NVSDK_NGX_Result (NVSDK_CONV *)(NVSDK_NGX_Handle *);

// The parameter block, addressed in MSVC's vtable order.
struct Params {
    NVSDK_NGX_Parameter *o = nullptr;
    template <class F> F slot(int i) const { return reinterpret_cast<F>((*reinterpret_cast<void ***>(o))[i]); }
    void set(const char *n, unsigned int v)    { slot<void (*)(void *, const char *, unsigned int)>(4)(o, n, v); }
    void set(const char *n, float v)           { slot<void (*)(void *, const char *, float)>(6)(o, n, v); }
    void set(const char *n, ID3D12Resource *v) { slot<void (*)(void *, const char *, ID3D12Resource *)>(1)(o, n, v); }
    void reset()                               { slot<void (*)(void *)>(16)(o); }
};

const char *result_name(uint32_t r)
{
    switch (r) {
    case 1: return "Success"; case 0xBAD00000: return "Fail";
    case 0xBAD00001: return "FeatureNotSupported"; case 0xBAD00002: return "PlatformError";
    case 0xBAD00004: return "FeatureNotFound"; case 0xBAD00005: return "InvalidParameter";
    case 0xBAD0000B: return "UnableToInitializeFeature"; case 0xBAD0000C: return "OutOfDate";
    case 0xBAD0000E: return "NotInitialized"; default: return "?";
    }
}

struct Gpu {
    ID3D12Device *dev = nullptr;
    ID3D12CommandQueue *queue = nullptr;
    ID3D12CommandAllocator *alloc = nullptr;
    ID3D12GraphicsCommandList *list = nullptr;
    ID3D12Fence *fence = nullptr;
    HANDLE fence_ev = nullptr;
    UINT64 fence_v = 0;

    bool begin() { return SUCCEEDED(alloc->Reset()) && SUCCEEDED(list->Reset(alloc, nullptr)); }
    bool submit_and_wait()
    {
        if (FAILED(list->Close())) { LOG("command list Close failed"); return false; }
        ID3D12CommandList *l = list;
        queue->ExecuteCommandLists(1, &l);
        const UINT64 v = ++fence_v;
        if (FAILED(queue->Signal(fence, v))) { LOG("queue Signal failed"); return false; }
        if (fence->GetCompletedValue() < v) {
            fence->SetEventOnCompletion(v, fence_ev);
            WaitForSingleObject(fence_ev, 60000);
        }
        if (fence->GetCompletedValue() < v) { LOG("fence timeout"); return false; }
        return true;
    }
    ID3D12Resource *texture(UINT w, UINT h, DXGI_FORMAT f, bool uav, D3D12_RESOURCE_STATES st)
    {
        D3D12_HEAP_PROPERTIES hp = {}; hp.Type = D3D12_HEAP_TYPE_DEFAULT;
        D3D12_RESOURCE_DESC d = {};
        d.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D; d.Width = w; d.Height = h;
        d.DepthOrArraySize = 1; d.MipLevels = 1; d.Format = f; d.SampleDesc.Count = 1;
        d.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
        d.Flags = uav ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE;
        ID3D12Resource *r = nullptr;
        if (FAILED(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, st, nullptr, IID_PPV_ARGS(&r)))) return nullptr;
        return r;
    }
    ID3D12Resource *buffer(UINT64 size, D3D12_HEAP_TYPE t, D3D12_RESOURCE_STATES st)
    {
        D3D12_HEAP_PROPERTIES hp = {}; hp.Type = t;
        D3D12_RESOURCE_DESC d = {};
        d.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; d.Width = size; d.Height = 1;
        d.DepthOrArraySize = 1; d.MipLevels = 1; d.Format = DXGI_FORMAT_UNKNOWN; d.SampleDesc.Count = 1;
        d.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        ID3D12Resource *r = nullptr;
        if (FAILED(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, st, nullptr, IID_PPV_ARGS(&r)))) return nullptr;
        return r;
    }
};

D3D12_RESOURCE_BARRIER transition(ID3D12Resource *r, D3D12_RESOURCE_STATES a, D3D12_RESOURCE_STATES b)
{
    D3D12_RESOURCE_BARRIER br = {};
    br.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    br.Transition.pResource = r; br.Transition.StateBefore = a; br.Transition.StateAfter = b;
    br.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    return br;
}
D3D12_TEXTURE_COPY_LOCATION placed(ID3D12Resource *res, UINT w, UINT h, DXGI_FORMAT f, UINT pitch)
{
    D3D12_TEXTURE_COPY_LOCATION l = {};
    l.pResource = res; l.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    l.PlacedFootprint.Footprint.Format = f; l.PlacedFootprint.Footprint.Width = w;
    l.PlacedFootprint.Footprint.Height = h; l.PlacedFootprint.Footprint.Depth = 1;
    l.PlacedFootprint.Footprint.RowPitch = pitch;
    return l;
}
D3D12_TEXTURE_COPY_LOCATION whole(ID3D12Resource *res)
{
    D3D12_TEXTURE_COPY_LOCATION l = {};
    l.pResource = res; l.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    return l;
}
template <class T> void release(T *&p) { if (p) { p->Release(); p = nullptr; } }

// Everything that belongs to one feature: the textures at its sizes, the
// staging buffers, and the handle.
struct Feature {
    uint32_t work_w = 0, work_h = 0, full_w = 0, full_h = 0, io_w = 0, io_h = 0;
    uint64_t motion_off = 0, output_off = 0, total = 0;
    UINT io_pitch = 0, work_pitch = 0;
    ID3D12Resource *color = nullptr, *motion = nullptr, *output = nullptr;
    ID3D12Resource *up_color = nullptr, *up_motion = nullptr, *readback = nullptr;
    NVSDK_NGX_Handle *handle = nullptr;
};

struct Server {
    Gpu gpu;
    HMODULE core = nullptr, runtime = nullptr;
    PFN_InitExt init_ext = nullptr; PFN_Create create = nullptr;
    PFN_Eval eval = nullptr; PFN_Release release_feature = nullptr;
    NVSDK_NGX_Parameter *praw = nullptr;
    Params p;
    wchar_t dir[MAX_PATH] = {};
    Feature f;
    uint8_t *shm = nullptr;
    HANDLE shm_file = INVALID_HANDLE_VALUE, shm_map = nullptr;
    uint64_t shm_size = 0;
    std::wstring shm_path;
    std::wstring dll_path = L"nvngx_dlssnr.dll";

    bool init_gpu();
    bool init_ngx();
    bool map_shm(uint64_t bytes);
    void unmap_shm();
    void destroy_feature();
    uint32_t build_feature(const NsNrCreate &c);
    uint32_t run_frame(const NsNrFrame &fr);
};

bool Server::init_gpu()
{
    HMODULE d3d12 = LoadLibraryW(L"d3d12.dll"), dxgi = LoadLibraryW(L"dxgi.dll");
    auto create_dev = d3d12 ? reinterpret_cast<PFN_D3D12_CREATE_DEVICE>(GetProcAddress(d3d12, "D3D12CreateDevice")) : nullptr;
    auto create_fac = dxgi ? reinterpret_cast<HRESULT (WINAPI *)(REFIID, void **)>(GetProcAddress(dxgi, "CreateDXGIFactory1")) : nullptr;
    if (!create_dev || !create_fac) { LOG("d3d12.dll / dxgi.dll exports missing - is vkd3d-proton in the prefix?"); return false; }
    IDXGIFactory4 *fac = nullptr;
    if (FAILED(create_fac(IID_PPV_ARGS(&fac)))) { LOG("CreateDXGIFactory1 failed"); return false; }
    IDXGIAdapter1 *ad = nullptr;
    for (UINT i = 0; fac->EnumAdapters1(i, &ad) == S_OK; ++i) {
        DXGI_ADAPTER_DESC1 d; ad->GetDesc1(&d);
        LOG("adapter %u: %ls vendor=0x%04X vram=%lluMB", i, d.Description, d.VendorId,
            static_cast<unsigned long long>(d.DedicatedVideoMemory >> 20));
        if (d.VendorId == 0x10DE) break;
        ad->Release(); ad = nullptr;
    }
    if (!ad) { LOG("no NVIDIA adapter - dxvk-nvapi needs DXVK_ENABLE_NVAPI=1 and the NVIDIA ICD"); return false; }
    const HRESULT hr = create_dev(ad, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&gpu.dev));
    if (FAILED(hr)) { LOG("D3D12CreateDevice failed 0x%08lX", hr); return false; }
    D3D12_COMMAND_QUEUE_DESC qd = {};
    if (FAILED(gpu.dev->CreateCommandQueue(&qd, IID_PPV_ARGS(&gpu.queue)))
        || FAILED(gpu.dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&gpu.alloc)))
        || FAILED(gpu.dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, gpu.alloc, nullptr, IID_PPV_ARGS(&gpu.list)))
        || FAILED(gpu.dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&gpu.fence)))) {
        LOG("D3D12 queue/list/fence creation failed"); return false;
    }
    gpu.list->Close();
    gpu.fence_ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    return true;
}

bool Server::init_ngx()
{
    core = LoadLibraryW(L"_nvngx.dll");
    if (!core) core = LoadLibraryW(L"nvngx.dll");
    if (!core) { LOG("NGX core (_nvngx.dll) did not load, err=%lu - the driver's Wine NGX is missing from the prefix", GetLastError()); return false; }
    auto ngx_init = reinterpret_cast<PFN_Init>(GetProcAddress(core, "NVSDK_NGX_D3D12_Init"));
    auto ngx_alloc = reinterpret_cast<PFN_Alloc>(GetProcAddress(core, "NVSDK_NGX_D3D12_AllocateParameters"));
    if (!ngx_init || !ngx_alloc) { LOG("NGX core exports missing"); return false; }
    NVSDK_NGX_Result r = ngx_init(0x1000000ULL, dir, gpu.dev, nullptr, NVSDK_NGX_Version_API);
    LOG("NVSDK_NGX_D3D12_Init -> 0x%08X (%s)", r, result_name(r));
    if (NVSDK_NGX_FAILED(r)) return false;
    r = ngx_alloc(&praw);
    if (NVSDK_NGX_FAILED(r) || !praw) { LOG("AllocateParameters -> 0x%08X", r); return false; }
    p.o = praw;

    runtime = LoadLibraryW(dll_path.c_str());
    if (!runtime) { LOG("%ls did not load, err=%lu", dll_path.c_str(), GetLastError()); return false; }
    init_ext = reinterpret_cast<PFN_InitExt>(GetProcAddress(runtime, "NVSDK_NGX_D3D12_Init_Ext"));
    create = reinterpret_cast<PFN_Create>(GetProcAddress(runtime, "NVSDK_NGX_D3D12_CreateFeature"));
    eval = reinterpret_cast<PFN_Eval>(GetProcAddress(runtime, "NVSDK_NGX_D3D12_EvaluateFeature"));
    release_feature = reinterpret_cast<PFN_Release>(GetProcAddress(runtime, "NVSDK_NGX_D3D12_ReleaseFeature"));
    if (!init_ext || !create || !eval || !release_feature) { LOG("nvngx_dlssnr.dll exports missing"); return false; }
    r = init_ext(0x1000000ULL, dir, gpu.dev, NVSDK_NGX_Version_API, praw);
    LOG("DLSSNR Init_Ext -> 0x%08X (%s)", r, result_name(r));
    return !NVSDK_NGX_FAILED(r);
}

bool Server::map_shm(uint64_t bytes)
{
    if (shm && shm_size >= bytes) return true;
    unmap_shm();
    shm_file = CreateFileW(shm_path.c_str(), GENERIC_READ | GENERIC_WRITE,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (shm_file == INVALID_HANDLE_VALUE) { LOG("cannot open the mapping %ls, err=%lu", shm_path.c_str(), GetLastError()); return false; }
    LARGE_INTEGER size = {};
    GetFileSizeEx(shm_file, &size);
    if (static_cast<uint64_t>(size.QuadPart) < bytes) {
        LOG("the mapping is %lld bytes, the feature needs %llu", static_cast<long long>(size.QuadPart),
            static_cast<unsigned long long>(bytes));
        return false;
    }
    shm_map = CreateFileMappingW(shm_file, nullptr, PAGE_READWRITE, 0, 0, nullptr);
    if (!shm_map) { LOG("CreateFileMapping failed, err=%lu", GetLastError()); return false; }
    shm = static_cast<uint8_t *>(MapViewOfFile(shm_map, FILE_MAP_ALL_ACCESS, 0, 0, 0));
    if (!shm) { LOG("MapViewOfFile failed, err=%lu", GetLastError()); return false; }
    shm_size = static_cast<uint64_t>(size.QuadPart);
    return true;
}

void Server::unmap_shm()
{
    if (shm) { UnmapViewOfFile(shm); shm = nullptr; }
    if (shm_map) { CloseHandle(shm_map); shm_map = nullptr; }
    if (shm_file != INVALID_HANDLE_VALUE) { CloseHandle(shm_file); shm_file = INVALID_HANDLE_VALUE; }
    shm_size = 0;
}

void Server::destroy_feature()
{
    if (f.handle) { release_feature(f.handle); f.handle = nullptr; }
    release(f.color); release(f.motion); release(f.output);
    release(f.up_color); release(f.up_motion); release(f.readback);
}

uint32_t Server::build_feature(const NsNrCreate &c)
{
    destroy_feature();
    f = Feature{};
    f.work_w = c.work_w; f.work_h = c.work_h; f.full_w = c.full_w; f.full_h = c.full_h;
    ns_nr_layout(f.work_w, f.work_h, f.full_w, f.full_h, &f.io_w, &f.io_h, &f.motion_off, &f.output_off, &f.total);
    if (f.work_w == 0 || f.work_h == 0) { LOG("create: zero size"); return 0xBAD00005; }
    if (!map_shm(f.total)) return 0xBAD00002;

    const bool upscale = f.io_w != f.work_w || f.io_h != f.work_h;
    const float ratio = upscale ? static_cast<float>(f.work_w) / static_cast<float>(f.io_w) : 1.0f;
    p.reset();
    p.set("CreationNodeMask", 1u); p.set("VisibilityNodeMask", 1u);
    p.set("DLSSNR.Width", f.work_w); p.set("DLSSNR.Height", f.work_h);
    p.set("DLSSNR.InputWidth", f.io_w); p.set("DLSSNR.InputHeight", f.io_h);
    p.set("DLSSNR.OutputWidth", f.io_w); p.set("DLSSNR.OutputHeight", f.io_h);
    p.set("DLSSNR.Output.Width", f.io_w); p.set("DLSSNR.Output.Height", f.io_h);
    p.set("DLSSNR.Upscaling", upscale ? 1u : 0u);
    p.set("DLSSNR.Scale", ratio); p.set("DLSSNR.ScalingRatio", ratio);
    p.set("DLSSNR.Hint.Render.Preset", c.preset);
    p.set("DLSS.Feature.Create.Flags", 0u);
    if (!gpu.begin()) return 0xBAD00002;
    const ULONGLONG t0 = GetTickCount64();
    NVSDK_NGX_Result r = create(gpu.list, static_cast<NVSDK_NGX_Feature>(18), praw, &f.handle);
    const bool submitted = gpu.submit_and_wait();
    LOG("CreateFeature(18) work %ux%u io %ux%u upscaling %s -> 0x%08X (%s) in %llu ms",
        f.work_w, f.work_h, f.io_w, f.io_h, upscale ? "on" : "off", r, result_name(r),
        static_cast<unsigned long long>(GetTickCount64() - t0));
    if (NVSDK_NGX_FAILED(r) || !f.handle || !submitted) { f.handle = nullptr; return NVSDK_NGX_FAILED(r) ? r : 0xBAD00002; }

    f.io_pitch = (f.io_w * 4 + 255) & ~255u;
    f.work_pitch = (f.work_w * 4 + 255) & ~255u;
    f.color = gpu.texture(f.io_w, f.io_h, DXGI_FORMAT_R8G8B8A8_UNORM, false, D3D12_RESOURCE_STATE_COPY_DEST);
    f.motion = gpu.texture(f.work_w, f.work_h, DXGI_FORMAT_R16G16_FLOAT, true, D3D12_RESOURCE_STATE_COPY_DEST);
    f.output = gpu.texture(f.io_w, f.io_h, DXGI_FORMAT_R8G8B8A8_UNORM, true, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    f.up_color = gpu.buffer(static_cast<UINT64>(f.io_pitch) * f.io_h, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    f.up_motion = gpu.buffer(static_cast<UINT64>(f.work_pitch) * f.work_h, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    f.readback = gpu.buffer(static_cast<UINT64>(f.io_pitch) * f.io_h, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
    if (!f.color || !f.motion || !f.output || !f.up_color || !f.up_motion || !f.readback) {
        LOG("texture/staging creation failed at %ux%u", f.io_w, f.io_h);
        destroy_feature();
        return 0xBAD00002;
    }
    return 1;
}

// Rows in, rows out: the mapping is tightly packed, D3D12 wants 256-byte
// row pitches, so every frame is two strided copies on the CPU. At 720p
// that is under a millisecond each.
void copy_rows(uint8_t *dst, size_t dst_pitch, const uint8_t *src, size_t src_pitch, size_t row_bytes, uint32_t rows)
{
    for (uint32_t y = 0; y < rows; ++y) memcpy(dst + y * dst_pitch, src + y * src_pitch, row_bytes);
}

uint32_t Server::run_frame(const NsNrFrame &fr)
{
    if (!f.handle) return 0xBAD0000E;
    uint8_t *m = nullptr;
    f.up_color->Map(0, nullptr, reinterpret_cast<void **>(&m));
    copy_rows(m, f.io_pitch, shm, static_cast<size_t>(f.io_w) * 4, static_cast<size_t>(f.io_w) * 4, f.io_h);
    f.up_color->Unmap(0, nullptr);
    f.up_motion->Map(0, nullptr, reinterpret_cast<void **>(&m));
    copy_rows(m, f.work_pitch, shm + f.motion_off, static_cast<size_t>(f.work_w) * 4, static_cast<size_t>(f.work_w) * 4, f.work_h);
    f.up_motion->Unmap(0, nullptr);

    if (!gpu.begin()) return 0xBAD00002;
    ID3D12GraphicsCommandList *list = gpu.list;
    D3D12_TEXTURE_COPY_LOCATION dc = whole(f.color), sc = placed(f.up_color, f.io_w, f.io_h, DXGI_FORMAT_R8G8B8A8_UNORM, f.io_pitch);
    D3D12_TEXTURE_COPY_LOCATION dm = whole(f.motion), sm = placed(f.up_motion, f.work_w, f.work_h, DXGI_FORMAT_R16G16_FLOAT, f.work_pitch);
    list->CopyTextureRegion(&dc, 0, 0, 0, &sc, nullptr);
    list->CopyTextureRegion(&dm, 0, 0, 0, &sm, nullptr);
    D3D12_RESOURCE_BARRIER in[2] = {
        transition(f.color, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        transition(f.motion, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE) };
    list->ResourceBarrier(2, in);

    p.reset();
    p.set("DLSSNR.Color", f.color); p.set("DLSSNR.Output", f.output); p.set("DLSSNR.MVec", f.motion);
    p.set("DLSSNR.ColorSubrectBaseX", 0u); p.set("DLSSNR.ColorSubrectBaseY", 0u);
    p.set("DLSSNR.ColorSubrectWidth", f.io_w); p.set("DLSSNR.ColorSubrectHeight", f.io_h);
    p.set("DLSSNR.MVecSubrectBaseX", 0u); p.set("DLSSNR.MVecSubrectBaseY", 0u);
    p.set("DLSSNR.MVecSubrectWidth", f.work_w); p.set("DLSSNR.MVecSubrectHeight", f.work_h);
    p.set("DLSSNR.OutputSubrectBaseX", 0u); p.set("DLSSNR.OutputSubrectBaseY", 0u);
    p.set("DLSSNR.OutputSubrectWidth", f.io_w); p.set("DLSSNR.OutputSubrectHeight", f.io_h);
    p.set("DLSSNR.MVecScaleX", 1.0f); p.set("DLSSNR.MVecScaleY", 1.0f);
    p.set("DLSSNR.Enabled", 1u); p.set("DLSSNR.Reset", fr.reset ? 1u : 0u);
    p.set("DLSSNR.Intensity", fr.intensity);
    p.set("DLSSNR.LocalToneStrength", fr.local_tone);
    p.set("DLSSNR.LocalStructureStrength", fr.local_structure);
    p.set("DLSSNR.SkinStructureStrength", fr.skin_structure);
    p.set("DLSSNR.UseAutoMask", fr.auto_mask); p.set("DLSSNR.Style", fr.style);
    p.set("DLSSNR.UICorrection", fr.ui_correction);
    p.set("DLSS.Pre.Exposure", 1.0f); p.set("DLSS.Exposure.Scale", fr.exposure);

    const NVSDK_NGX_Result r = eval(list, f.handle, praw, nullptr);

    D3D12_RESOURCE_BARRIER out[3] = {
        transition(f.output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE),
        transition(f.color, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST),
        transition(f.motion, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST) };
    list->ResourceBarrier(3, out);
    D3D12_TEXTURE_COPY_LOCATION drb = placed(f.readback, f.io_w, f.io_h, DXGI_FORMAT_R8G8B8A8_UNORM, f.io_pitch), so = whole(f.output);
    list->CopyTextureRegion(&drb, 0, 0, 0, &so, nullptr);
    D3D12_RESOURCE_BARRIER back = transition(f.output, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    list->ResourceBarrier(1, &back);
    if (!gpu.submit_and_wait()) return 0xBAD00002;
    if (NVSDK_NGX_FAILED(r)) return r;

    D3D12_RANGE range = { 0, static_cast<SIZE_T>(f.io_pitch) * f.io_h };
    f.readback->Map(0, &range, reinterpret_cast<void **>(&m));
    copy_rows(shm + f.output_off, static_cast<size_t>(f.io_w) * 4, m, f.io_pitch, static_cast<size_t>(f.io_w) * 4, f.io_h);
    D3D12_RANGE none = { 0, 0 };
    f.readback->Unmap(0, &none);
    return 1;
}

bool read_exact(void *dst, size_t n)
{
    uint8_t *d = static_cast<uint8_t *>(dst);
    while (n) {
        const size_t got = fread(d, 1, n, stdin);
        if (got == 0) return false;
        d += got; n -= got;
    }
    return true;
}

void reply(uint32_t magic, uint32_t seq, uint32_t ok, uint32_t ngx_result, uint32_t millis)
{
    NsNrReply r = { magic, seq, ok, ngx_result, millis, 0 };
    fwrite(&r, sizeof(r), 1, stdout);
    fflush(stdout);
}

// /dev/shm/x -> Z:\dev\shm\x, the drive Wine maps the Unix root to.
std::wstring windows_path(const char *unix_path)
{
    std::wstring w = L"Z:";
    for (const char *c = unix_path; *c; ++c) w += (*c == '/') ? L'\\' : static_cast<wchar_t>(*c);
    return w;
}

}  // namespace

int main(int argc, char **argv)
{
    if (argc < 2) { LOG("usage: nvngx.dll_nr.exe <mapping path>"); return 2; }
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
    Server s;
    s.shm_path = windows_path(argv[1]);
    if (argc > 2) s.dll_path = windows_path(argv[2]);
    GetModuleFileNameW(nullptr, s.dir, MAX_PATH);
    if (wchar_t *slash = wcsrchr(s.dir, L'\\')) *(slash + 1) = L'\0';

    const bool up = s.init_gpu() && s.init_ngx();
    reply(NS_NR_MAGIC_HELLO, 0, up ? 1u : 0u, 0, 0);
    if (!up) return 1;
    LOG("ready; mapping %ls", s.shm_path.c_str());

    uint32_t magic = 0;
    while (read_exact(&magic, sizeof(magic))) {
        if (magic == NS_NR_MAGIC_CREATE) {
            NsNrCreate c; c.magic = magic;
            if (!read_exact(reinterpret_cast<uint8_t *>(&c) + 4, sizeof(c) - 4)) break;
            const ULONGLONG t0 = GetTickCount64();
            const uint32_t r = s.build_feature(c);
            reply(NS_NR_MAGIC_CREATED, 0, r == 1 ? 1u : 0u, r, static_cast<uint32_t>(GetTickCount64() - t0));
        } else if (magic == NS_NR_MAGIC_FRAME) {
            NsNrFrame fr; fr.magic = magic;
            if (!read_exact(reinterpret_cast<uint8_t *>(&fr) + 4, sizeof(fr) - 4)) break;
            const ULONGLONG t0 = GetTickCount64();
            const uint32_t r = s.run_frame(fr);
            reply(NS_NR_MAGIC_DONE, fr.seq, r == 1 ? 1u : 0u, r, static_cast<uint32_t>(GetTickCount64() - t0));
        } else {
            LOG("unknown command 0x%08X - the stream is out of step", magic);
            break;
        }
    }
    s.destroy_feature();
    s.unmap_shm();
    LOG("stdin closed, exiting");
    return 0;
}
