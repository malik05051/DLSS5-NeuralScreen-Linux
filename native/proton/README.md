# native/proton — the neural renderer's Windows side

NVIDIA ships DLSSNR only as a D3D12 DLL, so this directory holds the one
piece of the program that is Windows code: `nvngx.dll_nr.exe`, a small
server that loads `nvngx_dlssnr.dll` under GE-Proton and evaluates frames
the Linux worker hands it (`native/linux/ns_proton.cpp`). The wire between
them is `ns_nr_wire.h`. `nvngx.dll_gate.exe` is the standalone feasibility
test the whole thing started from.

| File | What |
|---|---|
| `nvngx.dll_nr.cpp` | the server: D3D12 via vkd3d-proton, NGX core Init, the DLL's Init_Ext / CreateFeature / EvaluateFeature, pixels through a `/dev/shm` mapping |
| `nvngx.dll_gate.cpp` | standalone test: create feature 18, evaluate synthetic frames, write `gate_in.ppm` / `gate_out.ppm` |
| `ns_nr_wire.h` | the structs both sides read |
| `build.sh` | MinGW build of both exes |
| `setup.sh` | build, check the DLL, bootstrap the prefix with `umu-run`, run `neuralscreen-host --probe` |
| `nvngx_dlssnr.dll` | NVIDIA's runtime — **not in git**, put it here (or set `NS_NR_DLL`) |

The names carry `nvngx.dll` because the runtime refuses calls from any
module whose path does not. See TECHNICAL.md, "The neural renderer runs
through Proton", for the why of everything else.
