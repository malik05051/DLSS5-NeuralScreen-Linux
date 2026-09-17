// nvngx.dll_gate.exe - the feasibility gate for running nvngx_dlssnr.dll on
// Linux through Proton. Mirrors upstream's video path call for call:
//   NGX core Init -> AllocateParameters -> DLSSNR Init_Ext (direct) ->
//   CreateFeature(18) (direct) -> EvaluateFeature (direct) x N
// on synthetic frames, reads the result back and reports how much the
// network changed the picture. Everything goes to stderr; exit 0 = every
// evaluate succeeded and the output differs from the input.
//
// The file name carries "nvngx.dll" because the feature library refuses
// calls from a module whose path does not contain that substring.
#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cmath>
#include <vector>
#include <string>
#include "nvsdk_ngx.h"

// NVSDK_NGX_Parameter is an MSVC-built C++ interface. MSVC lays consecutive
// overloads out in REVERSE declaration order; GCC in declaration order. So
// the vtable is addressed by hand, in MSVC's order, instead of trusting the
// compiler's idea of which slot Set(ID3D12Resource*) lives in.
struct P {
    NVSDK_NGX_Parameter *o;
    template <class F> F slot(int i) const { return reinterpret_cast<F>((*reinterpret_cast<void ***>(o))[i]); }
    void Set(const char *n, unsigned int v)   { slot<void (*)(void *, const char *, unsigned int)>(4)(o, n, v); }
    void Set(const char *n, int v)            { slot<void (*)(void *, const char *, int)>(3)(o, n, v); }
    void Set(const char *n, float v)          { slot<void (*)(void *, const char *, float)>(6)(o, n, v); }
    void Set(const char *n, ID3D12Resource *v){ slot<void (*)(void *, const char *, ID3D12Resource *)>(1)(o, n, v); }
    void Reset()                              { slot<void (*)(void *)>(16)(o); }
    NVSDK_NGX_Parameter *operator->() { return o; }
};
#define LOG(...) do { fprintf(stderr, "[gate] " __VA_ARGS__); fputc('\n', stderr); fflush(stderr); } while (0)

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

static ID3D12Device *dev; static ID3D12CommandQueue *queue; static ID3D12CommandAllocator *alloc;
static ID3D12GraphicsCommandList *list; static ID3D12Fence *fence; static HANDLE fence_ev; static UINT64 fence_v;

static bool begin() { return SUCCEEDED(alloc->Reset()) && SUCCEEDED(list->Reset(alloc, nullptr)); }
static bool submit_wait()
{
    if (FAILED(list->Close())) { LOG("list Close failed"); return false; }
    ID3D12CommandList *l = list; queue->ExecuteCommandLists(1, &l);
    const UINT64 v = ++fence_v;
    if (FAILED(queue->Signal(fence, v))) { LOG("Signal failed"); return false; }
    if (fence->GetCompletedValue() < v) { fence->SetEventOnCompletion(v, fence_ev); WaitForSingleObject(fence_ev, 60000); }
    if (fence->GetCompletedValue() < v) { LOG("fence timeout"); return false; }
    return true;
}
static D3D12_RESOURCE_BARRIER tr(ID3D12Resource *r, D3D12_RESOURCE_STATES a, D3D12_RESOURCE_STATES b)
{
    D3D12_RESOURCE_BARRIER br = {}; br.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    br.Transition.pResource = r; br.Transition.StateBefore = a; br.Transition.StateAfter = b;
    br.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES; return br;
}
static ID3D12Resource *tex(UINT w, UINT h, DXGI_FORMAT f, bool uav, D3D12_RESOURCE_STATES st)
{
    D3D12_HEAP_PROPERTIES hp = {}; hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC d = {}; d.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D; d.Width = w; d.Height = h;
    d.DepthOrArraySize = 1; d.MipLevels = 1; d.Format = f; d.SampleDesc.Count = 1;
    d.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN; d.Flags = uav ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE;
    ID3D12Resource *r = nullptr;
    if (FAILED(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, st, nullptr, IID_PPV_ARGS(&r)))) return nullptr;
    return r;
}
static ID3D12Resource *buf(UINT64 size, D3D12_HEAP_TYPE t, D3D12_RESOURCE_STATES st)
{
    D3D12_HEAP_PROPERTIES hp = {}; hp.Type = t;
    D3D12_RESOURCE_DESC d = {}; d.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; d.Width = size; d.Height = 1;
    d.DepthOrArraySize = 1; d.MipLevels = 1; d.Format = DXGI_FORMAT_UNKNOWN; d.SampleDesc.Count = 1;
    d.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource *r = nullptr;
    if (FAILED(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, st, nullptr, IID_PPV_ARGS(&r)))) return nullptr;
    return r;
}
static const char *rn(uint32_t r)
{
    switch (r) {
    case 1: return "Success"; case 0xBAD00000: return "Fail"; case 0xBAD00001: return "FeatureNotSupported";
    case 0xBAD00002: return "PlatformError"; case 0xBAD00004: return "FeatureNotFound"; case 0xBAD00005: return "InvalidParameter";
    case 0xBAD0000B: return "UnableToInitializeFeature"; case 0xBAD0000C: return "OutOfDate"; case 0xBAD0000E: return "NotInitialized";
    case 0xBAD00012: return "OutOfDate(12)"; default: return "?"; }
}

int main(int argc, char **argv)
{
    const UINT W = argc > 1 ? atoi(argv[1]) : 640, H = argc > 2 ? atoi(argv[2]) : 360;
    const int N = argc > 3 ? atoi(argv[3]) : 10;
    wchar_t dir[MAX_PATH] = {}; GetModuleFileNameW(nullptr, dir, MAX_PATH);
    if (wchar_t *s = wcsrchr(dir, L'\\')) *(s + 1) = 0;

    // --- D3D12 on the NVIDIA adapter
    HMODULE d3d12 = LoadLibraryW(L"d3d12.dll"), dxgi = LoadLibraryW(L"dxgi.dll");
    auto create_dev = reinterpret_cast<PFN_D3D12_CREATE_DEVICE>(GetProcAddress(d3d12, "D3D12CreateDevice"));
    auto create_fac = reinterpret_cast<HRESULT (WINAPI *)(REFIID, void **)>(GetProcAddress(dxgi, "CreateDXGIFactory1"));
    if (!create_dev || !create_fac) { LOG("d3d12/dxgi exports missing"); return 10; }
    IDXGIFactory4 *fac = nullptr; create_fac(IID_PPV_ARGS(&fac));
    IDXGIAdapter1 *ad = nullptr;
    for (UINT i = 0; fac->EnumAdapters1(i, &ad) == S_OK; ++i) {
        DXGI_ADAPTER_DESC1 d; ad->GetDesc1(&d);
        LOG("adapter %u: %ls vendor=0x%04X vram=%lluMB", i, d.Description, d.VendorId, (unsigned long long)(d.DedicatedVideoMemory >> 20));
        if (d.VendorId == 0x10DE) break; ad->Release(); ad = nullptr;
    }
    if (!ad) { LOG("no NVIDIA adapter"); return 11; }
    HRESULT hr = create_dev(ad, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&dev));
    if (FAILED(hr)) { LOG("D3D12CreateDevice 0x%08lX", hr); return 12; }
    D3D12_COMMAND_QUEUE_DESC qd = {}; dev->CreateCommandQueue(&qd, IID_PPV_ARGS(&queue));
    dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&alloc));
    dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc, nullptr, IID_PPV_ARGS(&list)); list->Close();
    dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)); fence_ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    LOG("D3D12 device ready");

    // --- NGX core (the driver's, for Proton: _nvngx.dll), then the feature library
    HMODULE core = LoadLibraryW(L"_nvngx.dll"); if (!core) core = LoadLibraryW(L"nvngx.dll");
    if (!core) { LOG("NGX core did not load (%lu)", GetLastError()); return 13; }
    auto ngx_init = reinterpret_cast<PFN_Init>(GetProcAddress(core, "NVSDK_NGX_D3D12_Init"));
    auto ngx_alloc = reinterpret_cast<PFN_Alloc>(GetProcAddress(core, "NVSDK_NGX_D3D12_AllocateParameters"));
    if (!ngx_init || !ngx_alloc) { LOG("core exports missing"); return 13; }
    static NVSDK_NGX_FeatureCommonInfo common = {};
    common.LoggingInfo.LoggingCallback = [](const char *msg, NVSDK_NGX_Logging_Level, NVSDK_NGX_Feature) {
        fprintf(stderr, "[ngx] %s", msg); if (!msg[0] || msg[strlen(msg) - 1] != '\n') fputc('\n', stderr); fflush(stderr); };
    common.LoggingInfo.MinimumLoggingLevel = NVSDK_NGX_LOGGING_LEVEL_VERBOSE;
    common.LoggingInfo.DisableOtherLoggingSinks = false;
    NVSDK_NGX_Result r = ngx_init(0x1000000ULL, dir, dev, &common, NVSDK_NGX_Version_API);
    LOG("NVSDK_NGX_D3D12_Init -> 0x%08X (%s)", r, rn(r)); if (NVSDK_NGX_FAILED(r)) return 14;
    NVSDK_NGX_Parameter *praw = nullptr; r = ngx_alloc(&praw);
    LOG("AllocateParameters -> 0x%08X", r); if (NVSDK_NGX_FAILED(r) || !praw) return 15;
    P p{praw};

    HMODULE nr = LoadLibraryW(L"nvngx_dlssnr.dll");
    if (!nr) { LOG("nvngx_dlssnr.dll did not load (%lu)", GetLastError()); return 16; }
    auto init_ext = reinterpret_cast<PFN_InitExt>(GetProcAddress(nr, "NVSDK_NGX_D3D12_Init_Ext"));
    auto create = reinterpret_cast<PFN_Create>(GetProcAddress(nr, "NVSDK_NGX_D3D12_CreateFeature"));
    auto eval = reinterpret_cast<PFN_Eval>(GetProcAddress(nr, "NVSDK_NGX_D3D12_EvaluateFeature"));
    auto release = reinterpret_cast<PFN_Release>(GetProcAddress(nr, "NVSDK_NGX_D3D12_ReleaseFeature"));
    if (!init_ext || !create || !eval || !release) { LOG("feature exports missing"); return 16; }
    r = init_ext(0x1000000ULL, dir, dev, NVSDK_NGX_Version_API, praw);
    LOG("DLSSNR Init_Ext -> 0x%08X (%s)", r, rn(r)); if (NVSDK_NGX_FAILED(r)) return 17;

    // --- CreateFeature(18), 1:1 (no upscaling), the same contract as upstream
    p.Reset();
    p.Set("CreationNodeMask", 1u); p.Set("VisibilityNodeMask", 1u);
    p.Set("DLSSNR.Width", W); p.Set("DLSSNR.Height", H);
    p.Set("DLSSNR.InputWidth", W); p.Set("DLSSNR.InputHeight", H);
    p.Set("DLSSNR.OutputWidth", W); p.Set("DLSSNR.OutputHeight", H);
    p.Set("DLSSNR.Output.Width", W); p.Set("DLSSNR.Output.Height", H);
    p.Set("DLSSNR.Upscaling", 0u); p.Set("DLSSNR.Scale", 1.0f); p.Set("DLSSNR.ScalingRatio", 1.0f);
    p.Set("DLSSNR.Hint.Render.Preset", 0u); p.Set("DLSS.Feature.Create.Flags", 0u);
    NVSDK_NGX_Handle *feature = nullptr;
    if (!begin()) return 18;
    r = create(list, static_cast<NVSDK_NGX_Feature>(18), praw, &feature);
    const bool sub = submit_wait();
    LOG("CreateFeature(18) %ux%u -> 0x%08X (%s) handle=%p submit=%d", W, H, r, rn(r), (void *)feature, sub);
    if (NVSDK_NGX_FAILED(r) || !feature || !sub) return 2;

    // --- textures and staging
    ID3D12Resource *color = tex(W, H, DXGI_FORMAT_R8G8B8A8_UNORM, false, D3D12_RESOURCE_STATE_COPY_DEST);
    ID3D12Resource *mv = tex(W, H, DXGI_FORMAT_R16G16_FLOAT, true, D3D12_RESOURCE_STATE_COPY_DEST);
    ID3D12Resource *out = tex(W, H, DXGI_FORMAT_R8G8B8A8_UNORM, true, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    const UINT pitch = (W * 4 + 255) & ~255u;
    ID3D12Resource *up_c = buf(UINT64(pitch) * H, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    ID3D12Resource *up_m = buf(UINT64(pitch) * H, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    ID3D12Resource *rb = buf(UINT64(pitch) * H, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
    if (!color || !mv || !out || !up_c || !up_m || !rb) { LOG("resource creation failed"); return 19; }
    { void *m = nullptr; up_m->Map(0, nullptr, &m); memset(m, 0, size_t(pitch) * H); up_m->Unmap(0, nullptr); }

    std::vector<uint8_t> frame(size_t(W) * H * 4);
    uint32_t seed = 1; auto rnd = [&]() { seed = seed * 1664525u + 1013904223u; return int(seed >> 24); };
    auto place = [](ID3D12Resource *res, UINT w, UINT h, DXGI_FORMAT f, UINT pitch_) {
        D3D12_TEXTURE_COPY_LOCATION l = {}; l.pResource = res; l.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        l.PlacedFootprint.Footprint.Format = f; l.PlacedFootprint.Footprint.Width = w; l.PlacedFootprint.Footprint.Height = h;
        l.PlacedFootprint.Footprint.Depth = 1; l.PlacedFootprint.Footprint.RowPitch = pitch_; return l; };
    auto sub_ = [](ID3D12Resource *res) { D3D12_TEXTURE_COPY_LOCATION l = {}; l.pResource = res; l.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; return l; };

    int good = 0; double sum_diff = 0;
    for (int i = 0; i < N; ++i) {
        // gradient + hard stripes + noise: edges the network has to react to
        for (UINT y = 0; y < H; ++y) for (UINT x = 0; x < W; ++x) {
            uint8_t *px = &frame[(size_t(y) * W + x) * 4];
            int rr = x * 255 / W, gg = y * 255 / H, bb = 128;
            if ((y / 8) % 2 == 0) { rr /= 2; gg /= 2; bb /= 2; }
            int n = rnd() % 24 - 12;
            px[0] = uint8_t(std::min(255, std::max(0, rr + n))); px[1] = uint8_t(std::min(255, std::max(0, gg + n)));
            px[2] = uint8_t(std::min(255, std::max(0, bb + n))); px[3] = 255;
        }
        { uint8_t *m = nullptr; up_c->Map(0, nullptr, reinterpret_cast<void **>(&m));
          for (UINT y = 0; y < H; ++y) memcpy(m + size_t(y) * pitch, &frame[size_t(y) * W * 4], size_t(W) * 4);
          up_c->Unmap(0, nullptr); }

        if (!begin()) return 20;
        D3D12_TEXTURE_COPY_LOCATION dc = sub_(color), sc = place(up_c, W, H, DXGI_FORMAT_R8G8B8A8_UNORM, pitch);
        D3D12_TEXTURE_COPY_LOCATION dm = sub_(mv), sm = place(up_m, W, H, DXGI_FORMAT_R16G16_FLOAT, pitch);
        list->CopyTextureRegion(&dc, 0, 0, 0, &sc, nullptr);
        list->CopyTextureRegion(&dm, 0, 0, 0, &sm, nullptr);
        D3D12_RESOURCE_BARRIER b1[2] = { tr(color, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
                                         tr(mv, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE) };
        list->ResourceBarrier(2, b1);

        p.Reset();
        p.Set("DLSSNR.Color", color); p.Set("DLSSNR.Output", out); p.Set("DLSSNR.MVec", mv);
        p.Set("DLSSNR.ColorSubrectBaseX", 0u); p.Set("DLSSNR.ColorSubrectBaseY", 0u);
        p.Set("DLSSNR.ColorSubrectWidth", W); p.Set("DLSSNR.ColorSubrectHeight", H);
        p.Set("DLSSNR.MVecSubrectBaseX", 0u); p.Set("DLSSNR.MVecSubrectBaseY", 0u);
        p.Set("DLSSNR.MVecSubrectWidth", W); p.Set("DLSSNR.MVecSubrectHeight", H);
        p.Set("DLSSNR.OutputSubrectBaseX", 0u); p.Set("DLSSNR.OutputSubrectBaseY", 0u);
        p.Set("DLSSNR.OutputSubrectWidth", W); p.Set("DLSSNR.OutputSubrectHeight", H);
        p.Set("DLSSNR.MVecScaleX", 1.0f); p.Set("DLSSNR.MVecScaleY", 1.0f);
        p.Set("DLSSNR.Enabled", 1u); p.Set("DLSSNR.Reset", i == 0 ? 1u : 0u);
        p.Set("DLSSNR.Intensity", 1.0f); p.Set("DLSSNR.LocalToneStrength", 0.5f);
        p.Set("DLSSNR.LocalStructureStrength", 1.0f); p.Set("DLSSNR.SkinStructureStrength", -1.0f);
        p.Set("DLSSNR.UseAutoMask", 0u); p.Set("DLSSNR.Style", 1u); p.Set("DLSSNR.UICorrection", 0u);
        p.Set("DLSS.Pre.Exposure", 1.0f); p.Set("DLSS.Exposure.Scale", 1.0f);

        const ULONGLONG t0 = GetTickCount64();
        r = eval(list, feature, praw, nullptr);
        D3D12_RESOURCE_BARRIER b2[3] = { tr(out, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE),
                                         tr(color, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST),
                                         tr(mv, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST) };
        list->ResourceBarrier(3, b2);
        D3D12_TEXTURE_COPY_LOCATION drb = place(rb, W, H, DXGI_FORMAT_R8G8B8A8_UNORM, pitch), so = sub_(out);
        list->CopyTextureRegion(&drb, 0, 0, 0, &so, nullptr);
        D3D12_RESOURCE_BARRIER b3 = tr(out, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        list->ResourceBarrier(1, &b3);
        const bool ok = submit_wait();
        const ULONGLONG ms = GetTickCount64() - t0;

        double diff = 0, omean = 0, imean = 0; int dmax = 0;
        { uint8_t *m = nullptr; D3D12_RANGE rr = { 0, size_t(pitch) * H }; rb->Map(0, &rr, reinterpret_cast<void **>(&m));
          for (UINT y = 0; y < H; ++y) for (UINT x = 0; x < W * 4; ++x) { if (x % 4 == 3) continue;
              int a = m[size_t(y) * pitch + x], b = frame[(size_t(y) * W * 4) + x];
              diff += std::abs(a - b); dmax = std::max(dmax, std::abs(a - b)); omean += a; imean += b; }
          D3D12_RANGE none = { 0, 0 }; rb->Unmap(0, &none); }
        const double n = double(W) * H * 3; diff /= n; omean /= n; imean /= n;
        LOG("frame %d: eval -> 0x%08X (%s) submit=%d %llu ms | mean|out-in|=%.2f max=%d out-mean=%.1f in-mean=%.1f",
            i, r, rn(r), ok, (unsigned long long)ms, diff, dmax, omean, imean);
        if (!NVSDK_NGX_FAILED(r) && ok) { ++good; sum_diff += diff; }
        if (i == N - 1 && ok) {
            FILE *f = fopen("gate_out.ppm", "wb"); FILE *g = fopen("gate_in.ppm", "wb");
            if (f && g) { fprintf(f, "P6\n%u %u\n255\n", W, H); fprintf(g, "P6\n%u %u\n255\n", W, H);
                uint8_t *m = nullptr; D3D12_RANGE rr = { 0, size_t(pitch) * H }; rb->Map(0, &rr, reinterpret_cast<void **>(&m));
                for (UINT y = 0; y < H; ++y) for (UINT x = 0; x < W; ++x) { fwrite(m + size_t(y) * pitch + x * 4, 1, 3, f); fwrite(&frame[(size_t(y) * W + x) * 4], 1, 3, g); }
                D3D12_RANGE none = { 0, 0 }; rb->Unmap(0, &none); fclose(f); fclose(g); }
        }
    }
    release(feature);
    const bool changed = good > 0 && sum_diff / good > 0.5;
    LOG("verdict: %d/%d evaluates succeeded, mean change %.2f/255 -> %s", good, N, good ? sum_diff / good : 0.0,
        good == N && changed ? "DLSSNR RUNS ON THIS GPU UNDER PROTON" : "NOT WORKING");
    return good == N && changed ? 0 : 1;
}
