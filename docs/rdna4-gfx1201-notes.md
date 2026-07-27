# Operating notes — AMD RDNA4 (gfx1201)

One machine's specifics, kept because the *shape* of the problem generalises. Moved here from
obsidian-crucible's `docs/local-inference.md` §10 when the router graduated to this repo
(2026-07-26); the consumer-facing summary remains there.

- **ROCm is blocked, and not by the host.** Host ROCm 7.2.2 supports gfx1201 fine. The published
  AMD Infinity image ships torch `2.5.1+rocm6.2`, whose compiled arch list stops at
  `gfx1100`/`gfx942`; gfx1201 needs ROCm 6.4+. `torch.cuda.is_available()` still returns `True` on
  that image, so it passes its healthcheck and dies on the first kernel.
- **Do not force it with `HSA_OVERRIDE_GFX_VERSION`.** Presenting one architecture as another
  produces invalid GPU programs, which is the workload class that wedges this GPU hard enough to
  cost a desktop session. Signature, triggers, recovery paths (SSH or SysRq `S`-`U`-`B`; capture
  `/sys/class/drm/card1/device/devcoredump/data` *before* rebooting — it is in-memory only) are in
  `fleet-tools/references/rdna4-gpu-hang.md`. Read it before any GPU experiment on this class
  of hardware; it is deliberately not duplicated here.
- **Vulkan is not blocked.** RADV drives gfx1201 today and has run sustained embedding and rerank
  workloads without instability. The hazard is invalid GPU programs, not mature inference kernels.
- **The Mesa version is load-bearing.** gfx1201 needs Mesa ≥ 26. A container based on Debian
  trixie or Ubuntu 24.04 ships 24.x/25.0.x and falls back to `llvmpipe` *silently* (see
  `../llama/AGENTS.md`) — `debian:sid-slim` ships 26.1.5. "Stabilising" the base image is the
  failure, not the fix.
- **The bundled ROCm llama.cpp build has never been probed.** llama.cpp compiles for a different
  target set than PyTorch, so it may support gfx1201 where torch does not. Since Vulkan already
  works, this is optimisation rather than enablement.
