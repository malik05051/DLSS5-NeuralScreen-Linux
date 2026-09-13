"""GPU model and architecture, through NVML, with no external processes.

The interface needs it: to show what we are running on and whether Neural
Rendering is available. On Windows this asked nvapi, because nvapi is what
NVIDIA's own libraries ask and the answer therefore matched the decision
they make. NVML is the Linux equivalent and ships in the same driver
package - `libnvidia-ml.so.1` sits next to the kernel module - so nothing
here depends on the CUDA toolkit or on nvidia-smi being installed.

NVML does not expose an architecture id the way nvapi's GetArchInfo did; it
exposes a compute capability, which is the same information under a
different name. sm_75 is Turing, sm_86 Ampere, sm_89 Ada, sm_120 Blackwell -
and those are literally the kernels the NR runtime carries, so deriving the
verdict from them is closer to the truth than mapping through an
architecture table was.

Everything is wrapped in try: without NVML (a non-NVIDIA machine, the
nouveau driver, a container without the device nodes) the module returns
empty fields instead of taking the program down.
"""
from __future__ import annotations

import ctypes
import ctypes.util


# Compute capability (major, minor) -> (architecture, marketing family).
# Verified against the kernels inside the NR runtime itself: it carries
# sm_75/86/89/120, which is Turing through Blackwell.
ARCH_BY_CC = {
    (7, 5): ("Turing", "20xx"),
    (8, 0): ("Ampere", ""),
    (8, 6): ("Ampere", "30xx"),
    (8, 7): ("Ampere", ""),
    (8, 9): ("Ada", "40xx"),
    (9, 0): ("Hopper", ""),
    (10, 0): ("Blackwell", ""),
    (12, 0): ("Blackwell", "50xx"),
}

#: Neural Rendering officially requires Blackwell - see NGXGpuArchitecture
#: inside the NR runtime. Compute capability 10.0 is the first Blackwell.
CC_BLACKWELL = (10, 0)

#: Kept for the call sites that still speak in nvapi architecture groups
#: (the menu's verdict text, the tests). The numbers are nvapi's own.
ARCH_GROUPS = {
    "Turing": 0x160, "Ampere": 0x170, "Hopper": 0x180,
    "Ada": 0x190, "Blackwell": 0x1A0,
}
ARCH_BLACKWELL = 0x1A0


class _NVML:
    """The handful of NVML entry points this needs, bound lazily."""

    def __init__(self):
        self.lib = None
        for name in ("libnvidia-ml.so.1", "libnvidia-ml.so",
                     ctypes.util.find_library("nvidia-ml")):
            if not name:
                continue
            try:
                self.lib = ctypes.CDLL(name)
                break
            except OSError:
                continue
        self.ok = False
        if self.lib is None:
            return
        try:
            # nvmlInit_v2 is the current symbol; the unversioned one is a
            # compatibility shim that some stripped driver packages drop.
            init = getattr(self.lib, "nvmlInit_v2", None) or self.lib.nvmlInit
            self.ok = init() == 0
        except Exception:
            self.ok = False

    def shutdown(self) -> None:
        if self.ok and self.lib is not None:
            try:
                self.lib.nvmlShutdown()
            except Exception:
                pass
            self.ok = False


def _handles(nvml: _NVML) -> list:
    count = ctypes.c_uint(0)
    if nvml.lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0:
        return []
    out = []
    for index in range(count.value):
        handle = ctypes.c_void_p()
        if nvml.lib.nvmlDeviceGetHandleByIndex_v2(
                index, ctypes.byref(handle)) == 0:
            out.append(handle)
    return out


def _name(nvml: _NVML, handle) -> str:
    buf = ctypes.create_string_buffer(96)
    if nvml.lib.nvmlDeviceGetName(handle, buf, 96) != 0:
        return ""
    # "NVIDIA GeForce RTX 5070 Ti" -> "RTX 5070 Ti": the full name does not
    # fit the menu line, and the vendor adds nothing there.
    name = buf.value.decode("utf-8", "replace").strip()
    for prefix in ("NVIDIA GeForce ", "NVIDIA "):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _capability(nvml: _NVML, handle) -> tuple[int, int]:
    major, minor = ctypes.c_int(0), ctypes.c_int(0)
    if nvml.lib.nvmlDeviceGetCudaComputeCapability(
            handle, ctypes.byref(major), ctypes.byref(minor)) != 0:
        return (0, 0)
    return (major.value, minor.value)


def list_gpus() -> list[tuple[int, str]]:
    """Every NVIDIA card as [(index, name), ...], in NVML order.

    The index is the one the worker's NS_GPU takes; on Linux it is also the
    CUDA device order, so the menu, the log and the environment variable all
    mean the same number - one translation fewer than the DXGI adapter index
    needed on Windows.
    """
    nvml = _NVML()
    if not nvml.ok:
        return []
    try:
        return [(index, _name(nvml, handle) or f"NVIDIA device {index}")
                for index, handle in enumerate(_handles(nvml))]
    except Exception:
        return []
    finally:
        nvml.shutdown()


def probe(index: int = 0) -> dict:
    """{name, arch, arch_group, family, official} — empty fields on failure."""
    out = {"name": "", "arch": "", "arch_group": 0, "family": "",
           "official": False, "capability": (0, 0), "driver": ""}
    nvml = _NVML()
    if not nvml.ok:
        return out
    try:
        handles = _handles(nvml)
        if not handles or index >= len(handles):
            return out
        handle = handles[index]
        out["name"] = _name(nvml, handle)
        capability = _capability(nvml, handle)
        out["capability"] = capability
        arch, family = ARCH_BY_CC.get(capability, ("", ""))
        if not arch and capability[0]:
            # An architecture newer than this table: say Blackwell-or-later
            # rather than nothing. Being wrong about the marketing name is
            # better than a menu that claims not to know the card it is
            # running on.
            arch = "Blackwell" if capability >= CC_BLACKWELL else ""
        out["arch"] = arch
        out["family"] = family
        out["arch_group"] = ARCH_GROUPS.get(arch, 0)
        out["official"] = capability >= CC_BLACKWELL
        buf = ctypes.create_string_buffer(80)
        if nvml.lib.nvmlSystemGetDriverVersion(buf, 80) == 0:
            out["driver"] = buf.value.decode("ascii", "replace").strip()
    except Exception:
        return out
    finally:
        nvml.shutdown()
    return out


def describe(info: dict) -> str:
    """Menu line: "RTX 5070 Ti · Blackwell". Empty when the GPU is unknown —
    the menu shows its own placeholder rather than an English string in a
    localised interface."""
    name = info.get("name")
    if not name:
        return ""
    arch = info.get("arch")
    return f"{name} · {arch}" if arch else name


if __name__ == "__main__":
    got = probe()
    print(describe(got) or "unknown GPU")
    cc = got["capability"]
    print(f"compute capability {cc[0]}.{cc[1]}, driver {got['driver'] or '?'}, "
          f"officially supported: {'yes' if got['official'] else 'no'}")
    for index, name in list_gpus():
        print(f"  {index}: {name}")
