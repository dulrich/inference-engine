# ROCm gfx1201 spike — findings (2026-08-24)

Executed under the `rocm-gfx1201-spike` plan (watchdog-gated window, user save-work confirmation, `gpu-hang-watchdog.service` verified `active` before the first HIP dispatch and at window close). One bench window, ~35 min, GPU otherwise idle, `inference-engine-llama` stopped for the window and smoked green after.

## Verdict, one paragraph

Official ROCm 7.2.2 with native gfx1201 kernels ran **9 bench cells with zero kernel-journal noise and no hang** — the MES-wedge class documented in the fleet's `rdna4-gpu-hang` reference did not reproduce, consistent with its trigger being *invalid* GPU programs (arch overrides, missing-arch kernels), none of which exist on this stack anymore. On throughput, **Vulkan (RADV) remains the right production default** — it wins or ties everywhere except one workload class: **CPU-offloaded MoE (`-ncmoe`), where HIP is ~2.5× faster at prompt processing** and ~15% faster at decode. That class is exactly how production serves `qwen36-a3b` today, so a per-alias HIP experiment is a legitimate future plan; nothing changes now (production stays Vulkan per the root contract).

## Setup

- llama.cpp **b10534** (exact production-container tag), built twice on host from one checkout — backend is the only variable. GGUFs are the same files production bind-mounts (`$MODELS_DIR/...`).
- HIP: ROCm 7.2.2, `-DGGML_HIP=ON -DAMDGPU_TARGETS=gfx1201 -DGGML_HIP_ROCWMMA_FATTN=ON`; gfx1201 code objects verified via `llvm-objdump --offloading` before any dispatch. Runtime banner: `Device 0: AMD Radeon RX 9070, gfx1201 (0x1201), Wave Size: 32`.
- Vulkan: host RADV (Mesa 26.1.7 kisak), `KHR_coopmat` matrix cores. Host Mesa differs from the container's pinned Mesa — these numbers compare backends at one tag, not host-vs-container.
- Bench: `llama-bench -fa 1 -p 512,4096 -n 128 -r 3 -o json`, VRAM high-water sampled from `/sys/class/drm/card1/device/mem_info_vram_used` at 1 Hz. Raw JSON: scratch result files (untracked).

## Results (avg t/s over 3 reps)

| Model (quant, arch) | Backend | pp512 | pp4096 | tg128 | VRAM high-water |
|---|---|---|---|---|---|
| nemotron-3-nano-4b (Q4_K_M, dense) | Vulkan | 3048 | **4139** | **150.8** | 3.56 GiB |
| | HIP | **3163** | 3317 | 137.5 | 3.85 GiB |
| ling-3-tiny (Q4_K_M, MoE KDA/MLA) | Vulkan | **5910** | **6412** | **196.7** | 5.49 GiB |
| | HIP | 5173 | 4978 | 110.9 | 5.88 GiB |
| gemma-4-12b-qat (UD-Q4_K_XL, dense) | Vulkan | 2191 | **2068** | 66.3 | 8.07 GiB |
| | HIP | **2231** | 1852 | 64.1 | 8.35 GiB |
| qwen36-a3b (UD-Q4_K_XL, MoE, `-ncmoe 32` = production split) | Vulkan | 372 | 369 | 31.1 | 7.43 GiB |
| | HIP | **918** | **933** | **35.9** | 7.51 GiB |
| qwen36-a3b, full offload (22.4 GB file > 16 GB VRAM) | Vulkan | 603 | 591 | 45.0 | 15.79 GiB (GTT spill) |

Reading:

- **Dense and fully-resident MoE: Vulkan wins or ties.** Ling is the extreme case — Vulkan +77% decode; the KDA/MLA hybrid's HIP kernels ran *correctly* but slowly. This matches the public RDNA4 scoreboards (Vulkan wins decode at the 16 GB size class).
- **CPU-offloaded MoE is HIP's class.** With 32 expert layers on CPU, HIP's pp is 2.5× Vulkan's and tg leads by 15%. Plausible mechanism: the per-token host↔device traffic of the split dominates, and HIP's copy/dispatch path handles it far better than RADV's.
- **RADV over-commit quirk (watch for it in benchmarks):** full-offload qwen didn't fail — RADV silently spilled ~6 GB past VRAM into GTT and *decoded faster than the `-ncmoe` split* (45 vs 31 t/s). A full-offload HIP twin was deliberately not run (HIP has no transparent spill; expected clean OOM, no information gained for the risk). Any future Vulkan bench must pin the offload config or spill invalidates comparisons.
- VRAM: HIP consistently uses ~0.3–0.4 GiB more than Vulkan for the same cell.

## Stability

Zero occurrences of the MES signature family (`MES`, `evict`, `GPU reset`, `wedged`) in `journalctl -k` across the window, checked between every cell and at close. First-ever HIP dispatches on this card, including the immature-kernel-suspect KDA/MLA hybrid, all clean. The spike **did not demonstrate the hang** — which is itself the finding: valid, officially-supported kernels are not the trigger class. The watchdog stayed `active` throughout and was never exercised. The `HSA_OVERRIDE_GFX_VERSION` ban stands absolute (confirmed wedge trigger, never retested).

## vLLM (the gateway-exploration question)

Settled by the pre-plan recheck, no GPU time spent: vLLM mainline has **no native gfx1201 quant/FP8 kernels** — quantized models silently dequantize to FP32, bypassing RDNA4's WMMA units (community: vLLM-ROCm 48 t/s vs llama-server-Vulkan 62 t/s on a 9B Q6 on this card class). Community patches exist but are the "workload under development" hazard class. **Revisit when native gfx1201 kernels merge to mainline**; until then the local vLLM eval leg of `local-agent-tool-calling-gateway-exploration` should assume llama-server backends.

## Reproduction

```bash
# clone (production tag)
git clone --depth 1 --branch b10534 https://github.com/ggml-org/llama.cpp.git

# HIP build — NOTE: no /opt/rocm symlink on this host; llvm is under lib/llvm/bin
HIPCXX=/opt/rocm-7.2.2/lib/llvm/bin/clang++ HIP_PATH=/opt/rocm-7.2.2 \
cmake -S llama.cpp -B build-hip -DGGML_HIP=ON -DAMDGPU_TARGETS=gfx1201 \
  -DGGML_HIP_ROCWMMA_FATTN=ON -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/opt/rocm-7.2.2 \
  -DCMAKE_HIP_COMPILER=/opt/rocm-7.2.2/lib/llvm/bin/clang++ -DCMAKE_HIP_PLATFORM=amd
cmake --build build-hip --target llama-bench llama-server -j 20

# Vulkan build — b10534 find_package()s SPIRV-Headers but never links the target,
# so a non-system prefix needs -isystem; glslc from the distro package.
cmake -S llama.cpp -B build-vulkan -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=$PWD/spirv-headers-install \
  -DCMAKE_CXX_FLAGS="-isystem $PWD/spirv-headers-install/include"
cmake --build build-vulkan --target llama-bench llama-server -j 20

# one cell (see the scratch bench.sh while it survives; driver core:)
build-$backend/bin/llama-bench -m $model -fa 1 -p 512,4096 -n 128 -r 3 -o json [-ncmoe 32]
```

The full `bench.sh` driver (VRAM sampler + `HSA_OVERRIDE_GFX_VERSION` refusal guard) is archived with the WP records in the fleet's archive under `inference-engine/rocm-gfx1201-spike/`.

## Recommendation

1. Production serving stays Vulkan (contract; also the measured winner for 3 of 4 aliases).
2. Candidate future plan: HIP-backed serving experiment for **`-ncmoe`-class aliases only** (today: qwen36-a3b) — a 2.5× pp gain is user-visible on long-context tool-calling prompts. Requires its own plan (container build with ROCm runtime, alias-contract care, watchdog-gated canary).
3. Keep the ROCm ban lifted in its current watchdog-gated form; the hang reference's "no ROCm path" section is refreshed to point here.
