// ns_archspoof - tell the NGX runtime this card is a Blackwell.
//
// `nvngx_dlssnr` refuses to create feature 18 on anything below Blackwell.
// The refusal is a policy check, not missing code: the runtime carries
// sm_75/86/89/120 kernels, so a Turing, Ampere or Ada card has kernels it
// could run. The Windows build got past the check and a user confirmed it
// working on a 40-series card.
//
// On Windows the check was nvapi: the runtime loaded nvapi64.dll, took its
// single export `nvapi_QueryInterface`, asked for `NvAPI_GPU_GetArchInfo`
// by id, and the worker patched that one function in its own process
// memory. There is no nvapi here, and this file is the Linux equivalent of
// the same idea - with the notable improvement that it patches nothing.
// It is an LD_PRELOAD shim: the loader resolves the symbol to us first, we
// call the real one, and we change one number in the answer.
//
// **What this can and cannot know.** The Windows hook was written against a
// call this project had watched the runtime make. This one is written
// against NVML because NVML is how everything else on Linux asks that
// question - it is what `gpuinfo.py` uses and what
// `nvmlDeviceGetArchitecture` exists for. Whether the Linux NGX runtime
// asks that way has not been observed on hardware. So the shim does the one
// thing that makes the question answerable: it says, once, in the log,
// whether it was called at all. A run whose log has no `[spoof] asked`
// line is a run where NGX asked some other way, and that is a fact worth
// having rather than a silence.
//
// **What it deliberately does not touch.** The compute capability. Kernel
// selection reads that, and an Ampere card told it is sm_120 would be
// handed kernels it cannot execute - a crash inside NVIDIA's code instead
// of a clean refusal. The Windows hook rewrote the architecture only, for
// the same reason. `NS_ARCH_SPOOF_CC=1` enables it anyway for anyone
// experimenting; it is off by default and the log says when it is on.
//
// This likely conflicts with the licence terms of NVIDIA's redistributable.
// It defeats no copy protection and modifies no files. Enabling it is the
// user's call: `NS_ARCH_SPOOF=0` turns it off.
//
// Built by build-host.sh into libns-archspoof.so; pipeline.start_worker
// puts it in the worker's LD_PRELOAD and nothing else's.

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// nvmlDeviceArchitecture_t, from nvml.h. The values are ABI and have been
// stable since the enum appeared.
enum {
    NVML_DEVICE_ARCH_KEPLER = 2,
    NVML_DEVICE_ARCH_MAXWELL = 3,
    NVML_DEVICE_ARCH_PASCAL = 4,
    NVML_DEVICE_ARCH_VOLTA = 5,
    NVML_DEVICE_ARCH_TURING = 6,
    NVML_DEVICE_ARCH_AMPERE = 7,
    NVML_DEVICE_ARCH_ADA = 8,
    NVML_DEVICE_ARCH_HOPPER = 9,
    NVML_DEVICE_ARCH_BLACKWELL = 10,
    NVML_DEVICE_ARCH_UNKNOWN = 0xffffffffu,
};

typedef int (*pfn_get_arch)(void *device, unsigned int *arch);
typedef int (*pfn_get_cc)(void *device, int *major, int *minor);
typedef void *(*pfn_dlsym)(void *handle, const char *symbol);

static const char kArchSymbol[] = "nvmlDeviceGetArchitecture";
static const char kCcSymbol[] = "nvmlDeviceGetCudaComputeCapability";

static int enabled(void)
{
    const char *value = getenv("NS_ARCH_SPOOF");
    // On by default, as on Windows: a user who has installed this on an
    // Ampere card wants the feature to come up, and the alternative is a
    // program that starts and never processes a pixel.
    return !(value != NULL && value[0] == '0');
}

static int cc_enabled(void)
{
    const char *value = getenv("NS_ARCH_SPOOF_CC");
    return value != NULL && value[0] == '1';
}

static void note(const char *fmt, ...)
{
    // One line per distinct event, to stderr, which the worker's log picks
    // up. The point of these is diagnostic: they are how anyone finds out
    // which path the runtime actually took.
    va_list args;
    va_start(args, fmt);
    fprintf(stderr, "[spoof] ");
    vfprintf(stderr, fmt, args);
    fputc('\n', stderr);
    fflush(stderr);
    va_end(args);
}

static const char *arch_name(unsigned int arch)
{
    switch (arch) {
    case NVML_DEVICE_ARCH_KEPLER: return "Kepler";
    case NVML_DEVICE_ARCH_MAXWELL: return "Maxwell";
    case NVML_DEVICE_ARCH_PASCAL: return "Pascal";
    case NVML_DEVICE_ARCH_VOLTA: return "Volta";
    case NVML_DEVICE_ARCH_TURING: return "Turing";
    case NVML_DEVICE_ARCH_AMPERE: return "Ampere";
    case NVML_DEVICE_ARCH_ADA: return "Ada";
    case NVML_DEVICE_ARCH_HOPPER: return "Hopper";
    case NVML_DEVICE_ARCH_BLACKWELL: return "Blackwell";
    default: return "unknown";
    }
}

// The real dlsym, found without going through our own interposed one.
// dlvsym with the versioned name is the standard way out of that circle on
// glibc; RTLD_NEXT is the fallback for a libc that does not version it.
static pfn_dlsym real_dlsym(void)
{
    static pfn_dlsym cached;
    if (cached != NULL) return cached;
    cached = (pfn_dlsym)dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.2.5");
    if (cached == NULL) cached = (pfn_dlsym)dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.34");
    return cached;
}

static void *next_symbol(const char *name)
{
    pfn_dlsym base = real_dlsym();
    if (base != NULL) {
        void *found = base(RTLD_NEXT, name);
        if (found != NULL) return found;
    }
    // The runtime may have dlopen'd libnvidia-ml.so.1 itself, in which case
    // the symbol is not in our link chain at all. Open it and look there.
    static void *nvml;
    if (nvml == NULL) {
        nvml = dlopen("libnvidia-ml.so.1", RTLD_LAZY | RTLD_LOCAL);
        if (nvml == NULL) nvml = dlopen("libnvidia-ml.so", RTLD_LAZY | RTLD_LOCAL);
    }
    if (nvml != NULL && base != NULL) return base(nvml, name);
    return NULL;
}

// ---------------------------------------------------------------------------
// The one number that changes
// ---------------------------------------------------------------------------

int nvmlDeviceGetArchitecture(void *device, unsigned int *arch)
{
    static pfn_get_arch real;
    static int announced;

    if (real == NULL) real = (pfn_get_arch)next_symbol(kArchSymbol);
    if (real == NULL) return 1;   // NVML_ERROR_UNINITIALIZED

    const int rc = real(device, arch);
    if (rc != 0 || arch == NULL) return rc;

    if (!announced) {
        announced = 1;
        note("asked for the architecture: %s (%u)", arch_name(*arch), *arch);
    }
    if (!enabled()) return rc;

    // Only the three the runtime has kernels for. A Pascal card told it is
    // a Blackwell would get past the check and then fail with no kernels,
    // which is a worse failure than the honest one - and anything already
    // Blackwell or newer is left alone, exactly as the Windows hook
    // disabled itself on a 50-series card.
    if (*arch == NVML_DEVICE_ARCH_TURING || *arch == NVML_DEVICE_ARCH_AMPERE
        || *arch == NVML_DEVICE_ARCH_ADA) {
        static int said;
        if (!said) {
            said = 1;
            note("%s -> Blackwell (NS_ARCH_SPOOF=0 to disable)",
                 arch_name(*arch));
        }
        *arch = NVML_DEVICE_ARCH_BLACKWELL;
    }
    return rc;
}

int nvmlDeviceGetCudaComputeCapability(void *device, int *major, int *minor)
{
    static pfn_get_cc real;
    static int announced;

    if (real == NULL) real = (pfn_get_cc)next_symbol(kCcSymbol);
    if (real == NULL) return 1;

    const int rc = real(device, major, minor);
    if (rc != 0 || major == NULL || minor == NULL) return rc;

    if (!announced) {
        announced = 1;
        note("asked for the compute capability: sm_%d%d", *major, *minor);
    }
    // Off unless asked for, and loudly. This is the number kernel selection
    // reads: an Ampere card claiming 12.0 gets handed sm_120 kernels it
    // cannot execute, which is a crash inside NVIDIA's code rather than a
    // clean refusal. It exists because somebody experimenting will want it.
    if (enabled() && cc_enabled() && *major >= 7 && *major < 12) {
        static int said;
        if (!said) {
            said = 1;
            note("sm_%d%d -> sm_120 (NS_ARCH_SPOOF_CC is set; this is the "
                 "number kernel selection reads, expect a crash rather than "
                 "a refusal if it is wrong)", *major, *minor);
        }
        *major = 12;
        *minor = 0;
    }
    return rc;
}

// ---------------------------------------------------------------------------
// dlsym, so an explicit handle does not walk past us
// ---------------------------------------------------------------------------
//
// LD_PRELOAD only wins where the caller resolves through the ordinary
// lookup order. A library that dlopen's libnvidia-ml.so.1 and calls dlsym
// on that handle gets NVIDIA's function directly and never sees the two
// above. That is a plausible thing for the runtime to do - it is how it
// loads nvapi on Windows - so the lookup itself is interposed as well.

void *dlsym(void *handle, const char *symbol)
{
    pfn_dlsym base = real_dlsym();
    if (base == NULL) return NULL;
    // glibc marks symbol nonnull, so gcc objects to the check. It stays:
    // this function is called by code we do not control, and the
    // alternative to the check is strcmp dereferencing NULL.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wnonnull-compare"
    if (symbol != NULL && enabled()) {
#pragma GCC diagnostic pop
        if (strcmp(symbol, kArchSymbol) == 0) {
            static int said;
            if (!said) {
                said = 1;
                note("%s was looked up by handle - interposing", kArchSymbol);
            }
            return (void *)nvmlDeviceGetArchitecture;
        }
        if (cc_enabled() && strcmp(symbol, kCcSymbol) == 0) {
            return (void *)nvmlDeviceGetCudaComputeCapability;
        }
    }
    return base(handle, symbol);
}
