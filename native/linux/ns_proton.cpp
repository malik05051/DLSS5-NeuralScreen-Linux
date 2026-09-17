// ns_proton.cpp - see ns_proton.h.
#include "ns_proton.h"

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include <dirent.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../proton/ns_nr_wire.h"

namespace ns {

void log(const char *fmt, ...);   // host.cpp owns the log file
namespace {

bool is_file(const std::string &path)
{
    struct stat st{};
    return stat(path.c_str(), &st) == 0 && S_ISREG(st.st_mode);
}

bool is_dir(const std::string &path)
{
    struct stat st{};
    return stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}

std::string home_dir()
{
    const char *home = getenv("HOME");
    return home ? home : ".";
}

// The newest GE-Proton (or any Proton with a files/bin/wine) installed for
// Steam. "Newest" by name, which for GE-ProtonNN-M sorts the way versions
// do; good enough for a default the user can override.
std::string find_proton_wine()
{
    const std::string roots[] = {
        home_dir() + "/.local/share/Steam/compatibilitytools.d",
        home_dir() + "/.steam/root/compatibilitytools.d",
        home_dir() + "/.local/share/umu/compatibilitytools",
    };
    std::string best;
    for (const std::string &root : roots) {
        DIR *dir = opendir(root.c_str());
        if (dir == nullptr) continue;
        while (dirent *entry = readdir(dir)) {
            const std::string name(entry->d_name);
            if (name.rfind("GE-Proton", 0) != 0 && name.rfind("Proton", 0) != 0
                && name.rfind("UMU-Proton", 0) != 0) continue;
            const std::string wine = root + "/" + name + "/files/bin/wine";
            if (!is_file(wine)) continue;
            // GE-Proton first, then whatever sorts last.
            const bool ge = name.rfind("GE-Proton", 0) == 0;
            const bool best_ge = best.find("/GE-Proton") != std::string::npos;
            if (best.empty() || (ge && !best_ge) || (ge == best_ge && wine > best))
                best = wine;
        }
        closedir(dir);
    }
    return best;
}

}  // namespace

ProtonNr::~ProtonNr() { stop(); }

bool ProtonNr::locate(const std::string &exe_dir, std::string *exe,
                      std::string *wine, std::string *prefix,
                      std::string *describe)
{
    std::string e;
    if (const char *v = getenv("NS_NR_EXE")) e = v;
    else {
        const std::string candidates[] = {
            exe_dir + "/../proton/nvngx.dll_nr.exe",
            exe_dir + "/nvngx.dll_nr.exe",
            "native/proton/nvngx.dll_nr.exe",
        };
        for (const std::string &c : candidates) if (is_file(c)) { e = c; break; }
    }
    std::string w;
    if (const char *v = getenv("NS_PROTON_WINE")) w = v;
    else w = find_proton_wine();
    std::string p;
    if (const char *v = getenv("NS_PROTON_PREFIX")) p = v;
    else p = home_dir() + "/.local/share/neuralscreen/pfx";

    std::string dll;
    if (!e.empty()) {
        const size_t slash = e.rfind('/');
        dll = (slash == std::string::npos ? std::string(".") : e.substr(0, slash))
              + "/nvngx_dlssnr.dll";
    }
    if (describe) {
        *describe = "exe: " + (e.empty() ? std::string("not found") : e)
                    + "; runtime dll: " + (dll.empty() ? std::string("-") : (is_file(dll) ? dll : dll + " (MISSING)"))
                    + "; wine: " + (w.empty() ? std::string("not found") : w)
                    + "; prefix: " + p + (is_dir(p + "/drive_c") ? "" : " (not bootstrapped)");
    }
    if (exe) *exe = e;
    if (wine) *wine = w;
    if (prefix) *prefix = p;
    return !e.empty() && is_file(dll) && !w.empty() && is_dir(p + "/drive_c");
}

bool ProtonNr::start(const std::string &exe_dir, std::string *error)
{
    if (running()) return true;
    std::string exe, wine, prefix, describe;
    if (!locate(exe_dir, &exe, &wine, &prefix, &describe)) {
        if (error) *error = "Proton neural renderer is not set up (" + describe + ")";
        return false;
    }
    // The mapping: a file on /dev/shm this side creates and grows, and the
    // child opens through Z:\dev\shm. Sized at create_feature.
    char name[64];
    snprintf(name, sizeof(name), "/dev/shm/neuralscreen-nr-%d", static_cast<int>(getpid()));
    shm_path_ = name;
    shm_fd_ = open(shm_path_.c_str(), O_RDWR | O_CREAT | O_TRUNC, 0600);
    if (shm_fd_ < 0) {
        if (error) *error = std::string("cannot create ") + shm_path_ + ": " + strerror(errno);
        return false;
    }

    int in[2], out[2];
    if (pipe(in) != 0 || pipe(out) != 0) {
        if (error) *error = std::string("pipe: ") + strerror(errno);
        return false;
    }
    const std::string exe_parent = exe.substr(0, exe.rfind('/') == std::string::npos ? 0 : exe.rfind('/'));
    // GE-Proton's wine lives in files/bin; its libraries in files/lib.
    const std::string files = wine.substr(0, wine.rfind("/bin/wine"));
    const std::string arch = getenv("NS_PROTON_ARCH") ? getenv("NS_PROTON_ARCH") : "GB200";

    const pid_t pid = fork();
    if (pid < 0) {
        if (error) *error = std::string("fork: ") + strerror(errno);
        return false;
    }
    if (pid == 0) {
        dup2(in[0], 0);
        dup2(out[1], 1);
        close(in[0]); close(in[1]); close(out[0]); close(out[1]);
        if (!exe_parent.empty()) (void)!chdir(exe_parent.c_str());
        setenv("WINEPREFIX", prefix.c_str(), 1);
        setenv("WINEDLLPATH", (files + "/lib/wine").c_str(), 1);
        const std::string path = files + "/bin:" + (getenv("PATH") ? getenv("PATH") : "");
        setenv("PATH", path.c_str(), 1);
        // vkd3d-proton, DXVK's dxgi and dxvk-nvapi are what the prefix was
        // bootstrapped with; the driver's NGX core for Wine likewise.
        setenv("WINEDLLOVERRIDES", "d3d12,d3d12core,dxgi,nvapi64,nvngx,_nvngx=n,b", 1);
        setenv("WINEESYNC", "1", 0);
        setenv("WINEFSYNC", "1", 0);
        setenv("DXVK_ENABLE_NVAPI", "1", 1);
        setenv("DXVK_NVAPI_GPU_ARCH", arch.c_str(), 0);
        setenv("WINEDEBUG", "-all", 0);
        // The child's stderr is our log; DXVK and vkd3d-proton's startup
        // banners are not what anyone opens it for.
        setenv("DXVK_LOG_LEVEL", "warn", 0);
        setenv("VKD3D_DEBUG", "err", 0);
        // The NVML shim is for the native NGX core; Wine must not inherit it.
        unsetenv("LD_PRELOAD");
        execl(wine.c_str(), wine.c_str(), exe.c_str(), shm_path_.c_str(), static_cast<char *>(nullptr));
        _exit(127);
    }
    close(in[0]); close(out[1]);
    to_child_ = in[1];
    from_child_ = out[0];
    pid_ = pid;
    log("[proton] started %s (wine %s, %s as GPU)", exe.c_str(), wine.c_str(), arch.c_str());

    uint32_t ok = 0, result = 0, millis = 0;
    if (!read_reply(NS_NR_MAGIC_HELLO, &ok, &result, &millis) || !ok) {
        if (error) *error = "the Proton neural renderer did not come up - see its log lines above";
        stop();
        return false;
    }
    return true;
}

void ProtonNr::stop()
{
    if (to_child_ >= 0) { close(to_child_); to_child_ = -1; }
    if (from_child_ >= 0) { close(from_child_); from_child_ = -1; }
    if (pid_ > 0) {
        // EOF on its stdin is the request; give it a moment, then insist.
        int status = 0;
        for (int i = 0; i < 50 && waitpid(pid_, &status, WNOHANG) == 0; ++i) usleep(100000);
        if (waitpid(pid_, &status, WNOHANG) == 0) { kill(pid_, SIGKILL); waitpid(pid_, &status, 0); }
        pid_ = -1;
    }
    unmap_shared();
    if (shm_fd_ >= 0) { close(shm_fd_); shm_fd_ = -1; }
    if (!shm_path_.empty()) { unlink(shm_path_.c_str()); shm_path_.clear(); }
    has_feature_ = false;
}

bool ProtonNr::write_all(const void *data, size_t size)
{
    const auto *p = static_cast<const uint8_t *>(data);
    while (size > 0) {
        const ssize_t n = write(to_child_, p, size);
        if (n <= 0) { if (n < 0 && errno == EINTR) continue; return false; }
        p += n; size -= static_cast<size_t>(n);
    }
    return true;
}

bool ProtonNr::read_reply(uint32_t expect_magic, uint32_t *ok, uint32_t *result,
                          uint32_t *millis)
{
    NsNrReply r{};
    auto *p = reinterpret_cast<uint8_t *>(&r);
    size_t left = sizeof(r);
    while (left > 0) {
        const ssize_t n = read(from_child_, p, left);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) continue;
            log("[proton] the neural renderer process stopped answering");
            return false;
        }
        p += n; left -= static_cast<size_t>(n);
    }
    if (r.magic != expect_magic) {
        log("[proton] expected reply 0x%08X, got 0x%08X - out of step", expect_magic, r.magic);
        return false;
    }
    *ok = r.ok; *result = r.ngx_result; *millis = r.millis;
    return true;
}

bool ProtonNr::map_shared(uint64_t bytes)
{
    if (shm_ != nullptr && shm_size_ >= bytes) return true;
    unmap_shared();
    if (ftruncate(shm_fd_, static_cast<off_t>(bytes)) != 0) return false;
    void *p = mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd_, 0);
    if (p == MAP_FAILED) return false;
    shm_ = static_cast<uint8_t *>(p);
    shm_size_ = bytes;
    return true;
}

void ProtonNr::unmap_shared()
{
    if (shm_ != nullptr) { munmap(shm_, shm_size_); shm_ = nullptr; }
    shm_size_ = 0;
}

void ProtonNr::release_staging(Device &device)
{
    device.destroy_image(staging_in_);
    device.destroy_image(staging_out_);
    device.destroy_buffer(down_color_);
    device.destroy_buffer(down_motion_);
    device.destroy_buffer(up_output_);
}

bool ProtonNr::create_feature(Device &device, uint32_t work_w, uint32_t work_h,
                              uint32_t full_w, uint32_t full_h)
{
    if (!running()) return false;
    release_feature(device);
    uint64_t total = 0;
    ns_nr_layout(work_w, work_h, full_w, full_h, &io_w_, &io_h_, &motion_off_, &output_off_, &total);
    work_w_ = work_w; work_h_ = work_h;
    if (!map_shared(total)) {
        log("[proton] cannot size the shared mapping to %llu bytes", static_cast<unsigned long long>(total));
        last_result_ = 0xBAD00002;
        return false;
    }
    if (!device.make_image(staging_in_, io_w_, io_h_, VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_USAGE_TRANSFER_SRC_BIT)
        || !device.make_image(staging_out_, io_w_, io_h_, VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_USAGE_TRANSFER_DST_BIT)
        || !device.make_buffer(down_color_, static_cast<VkDeviceSize>(io_w_) * io_h_ * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT, true)
        || !device.make_buffer(down_motion_, static_cast<VkDeviceSize>(work_w) * work_h * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT, true)
        || !device.make_buffer(up_output_, static_cast<VkDeviceSize>(io_w_) * io_h_ * 4, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, true)) {
        log("[proton] staging creation failed at %ux%u", io_w_, io_h_);
        release_staging(device);
        last_result_ = 0xBAD00002;
        return false;
    }

    NsNrCreate c{};
    c.magic = NS_NR_MAGIC_CREATE;
    c.work_w = work_w; c.work_h = work_h; c.full_w = full_w; c.full_h = full_h;
    c.preset = 0;
    uint32_t ok = 0, result = 0, millis = 0;
    if (!write_all(&c, sizeof(c)) || !read_reply(NS_NR_MAGIC_CREATED, &ok, &result, &millis)) {
        last_result_ = 0xBAD00002;
        release_staging(device);
        return false;
    }
    last_result_ = result;
    last_millis_ = millis;
    has_feature_ = ok != 0;
    if (!has_feature_) {
        log("[proton] CreateFeature(18) refused: 0x%08X", result);
        release_staging(device);
        return false;
    }
    log("[proton] feature 18 created in the Proton process: work %ux%u, io %ux%u, %u ms",
        work_w, work_h, io_w_, io_h_, millis);
    seq_ = 0;
    return true;
}

void ProtonNr::release_feature(Device &device)
{
    // The child replaces its feature on the next create; nothing to tell it.
    if (has_feature_) vkDeviceWaitIdle(device.handle());
    release_staging(device);
    has_feature_ = false;
}

bool ProtonNr::evaluate(Device &device, Image &color, Image &output, Image &motion,
                        const NrOptions &options, bool reset)
{
    if (!has_feature_ || shm_ == nullptr) return false;

    // Out: colour (16F -> 8-bit by the blit) and motion, to host memory.
    VkCommandBuffer cmd = device.begin();
    if (cmd == VK_NULL_HANDLE) return false;
    device.blit(cmd, color, staging_in_, false);
    device.copy_image_to_buffer(cmd, staging_in_, down_color_);
    device.copy_image_to_buffer(cmd, motion, down_motion_);
    if (!device.submit_and_wait(cmd)) return false;
    memcpy(shm_, down_color_.mapped, static_cast<size_t>(io_w_) * io_h_ * 4);
    memcpy(shm_ + motion_off_, down_motion_.mapped, static_cast<size_t>(work_w_) * work_h_ * 4);

    // Across.
    NsNrFrame f{};
    f.magic = NS_NR_MAGIC_FRAME;
    f.seq = ++seq_;
    f.reset = reset ? 1u : 0u;
    f.style = options.style; f.auto_mask = options.auto_mask; f.ui_correction = options.ui_correction;
    f.intensity = options.intensity; f.local_tone = options.local_tone;
    f.local_structure = options.local_structure; f.skin_structure = options.skin_structure;
    f.exposure = options.exposure;
    uint32_t ok = 0, result = 0, millis = 0;
    if (!write_all(&f, sizeof(f)) || !read_reply(NS_NR_MAGIC_DONE, &ok, &result, &millis)) {
        last_result_ = 0xBAD00002;
        return false;
    }
    last_result_ = result;
    last_millis_ = millis;
    if (!ok) return false;

    // Back: the result into the host's float output image.
    memcpy(up_output_.mapped, shm_ + output_off_, static_cast<size_t>(io_w_) * io_h_ * 4);
    cmd = device.begin();
    if (cmd == VK_NULL_HANDLE) return false;
    device.copy_buffer_to_image(cmd, up_output_, staging_out_);
    device.blit(cmd, staging_out_, output, false);
    return device.submit_and_wait(cmd);
}

}  // namespace ns
