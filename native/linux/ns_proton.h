// ns_proton.h - the neural renderer through Proton.
//
// NVIDIA ships DLSSNR only as a D3D12 DLL (nvngx_dlssnr.dll), and there is
// no Linux snippet for NGX to load - not in the driver, not on NVIDIA's OTA
// server. So on Linux the network runs where the DLL can: in a Windows
// process under Proton, nvngx.dll_nr.exe (native/proton), with vkd3d-proton
// underneath it and the same NVIDIA driver at the bottom.
//
// This class is that process from the host's side. It presents the same
// three steps as the native Ngx class - create the feature, evaluate,
// release - and hides the rest: the frame goes out through a file mapping
// on /dev/shm (which Wine maps to the same pages), the answer comes back
// through the same mapping, and a few bytes of control travel down the
// child's stdin and stdout. Per frame that is one GPU readback and one
// upload on this side; the Windows side does the mirror image.
#pragma once

#include <cstdint>
#include <string>
#include <sys/types.h>

#include "ns_vk.h"

namespace ns {

class ProtonNr {
public:
    ~ProtonNr();

    // Where the pieces are. Every one can be overridden by environment:
    //   NS_NR_EXE      the Windows-side program (default: ../proton/nvngx.dll_nr.exe
    //                  relative to this binary, then native/proton/)
    //   NS_NR_DLL      nvngx_dlssnr.dll (default: beside the exe)
    //   NS_PROTON_WINE the wine binary to run it with (default: the newest
    //                  GE-Proton under ~/.local/share/Steam/compatibilitytools.d)
    //   NS_PROTON_PREFIX the WINEPREFIX (default: ~/.local/share/neuralscreen/pfx)
    //   NS_PROTON_ARCH   what dxvk-nvapi reports as the GPU (default GB200:
    //                  the runtime refuses everything older, see TECHNICAL.md)
    // `describe` says what was found, for the log and --probe.
    static bool locate(const std::string &exe_dir, std::string *exe,
                       std::string *wine, std::string *prefix,
                       std::string *describe, std::string *runtime_dll = nullptr);

    // Start the child and wait for its hello. False with `error` set when
    // the pieces are missing or the child could not bring up D3D12/NGX.
    bool start(const std::string &exe_dir, std::string *error);
    void stop();
    bool running() const { return pid_ > 0; }

    // The same contract as Ngx: work is the network's size, full the frame's.
    bool create_feature(Device &device, uint32_t work_w, uint32_t work_h,
                        uint32_t full_w, uint32_t full_h);
    void release_feature(Device &device);
    bool has_feature() const { return has_feature_; }
    uint32_t last_result() const { return last_result_; }
    uint32_t last_millis() const { return last_millis_; }

    // One frame, synchronously: records and submits its own command
    // buffers, because the round trip through the other process sits in
    // the middle of the GPU work.
    bool evaluate(Device &device, Image &color, Image &output, Image &motion,
                  const NrOptions &options, bool reset);

private:
    bool write_all(const void *data, size_t size);
    bool read_reply(uint32_t expect_magic, uint32_t *ok, uint32_t *result,
                    uint32_t *millis);
    bool map_shared(uint64_t bytes);
    void unmap_shared();
    void release_staging(Device &device);

    pid_t pid_ = -1;
    int to_child_ = -1, from_child_ = -1;
    std::string shm_path_;
    int shm_fd_ = -1;
    uint8_t *shm_ = nullptr;
    uint64_t shm_size_ = 0;

    uint32_t work_w_ = 0, work_h_ = 0, io_w_ = 0, io_h_ = 0;
    uint64_t motion_off_ = 0, output_off_ = 0;
    bool has_feature_ = false;
    uint32_t last_result_ = 0, last_millis_ = 0, seq_ = 0;

    // This side's staging: an RGBA8 copy of the colour image and one of the
    // result (the network works in 8-bit, the host's images are 16-bit
    // float; the blits convert), and the host-visible buffers under them.
    Image staging_in_, staging_out_;
    Buffer down_color_, down_motion_, up_output_;
};

}  // namespace ns
