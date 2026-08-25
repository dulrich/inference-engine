# gfx1201 spike 2 — KV-quant × flash-attention × backend + MTP findings (2026-08-25)

Executed under `plans/gfx1201-kv-fa-mtp-spike.md` (one watchdog-gated window, user save-work confirmation, `gpu-hang-watchdog.service` verified `active` before the first HIP dispatch and at close). Follows `docs/rocm-gfx1201-spike-findings.md` (spike 1); same instruments — llama.cpp **b10534** built twice on host (HIP gfx1201 + rocWMMA FA; Vulkan RADV/KHR_coopmat), the same GGUF files production bind-mounts.

## Verdict, one paragraph

The spike-1 pattern — **HIP wins wherever the workload leaves the big-fused-matmul path** — is now confirmed on a second axis: with a 16384-token pre-filled cache, HIP's prompt throughput is *flat across KV cache types* on all three models (its CUDA-lineage quantized-KV flash-attention kernels hold), while Vulkan degrades −29% to −63% the moment the cache is quantized (worst: MLA q8_0, 3701 → 1358 t/s). But Vulkan-f16 still wins or ties everything except the CPU-offloaded-MoE class, and quantized KV buys almost no VRAM at these context sizes (≤0.53 GiB at 16k), so **f16 KV stays the fleet default** and the planned quality leg was not justified by any config worth adopting. The actionable discovery is **MTP**: b10534 serves `qwen35` multi-token prediction from the on-disk unsloth Qwen3.8-27B GGUFs first try — **lossless +35% decode on Vulkan (36.3 → 48.9 t/s) and +68% on HIP (24.0 → 40.2)** at the cost of ~+2.4 GiB draft-context VRAM. That is a flag-level (`--spec-type draft-mtp`) serving-recipe dimension available to the bakeoff today.

## Setup

- llama.cpp b10534, host builds from spike 1 (`runs/rocm-spike/build-{hip,vulkan}/bin/`), reproduction recipe in the spike-1 findings doc.
- KV matrix: `llama-bench -fa 1 -p 2048 -n 64 -d 0,16384 -r 3 -o json` per cell (`bench-kv.sh`); `@16384` columns are measured with a pre-filled 16384-token cache — where KV-read cost actually shows. Symmetric KV types only ({f16, q8_0/q8_0, q4_0/q4_0}); asymmetric K/V silently falls back to the non-fused attention path on all backends and was excluded by design.
- MTP A/B: `llama-server -ngl 99 -fa on -c 8192` ± `--spec-type draft-mtp` (`mtp-ab.sh`), 3 fixed generation prompts at temperature 0, 384 tokens each; throughput and acceptance read from the response-body `timings` (probe by body, never status code).
- **llama-bench flag trap (measured)**: `-p/-n/-d` **accumulate** across repeats (b10534 `llama-bench.cpp` uses `insert`, not assign) — appending them to a wrapper's baked flags multiplies cells instead of overriding. `bench-kv.sh` refuses them as passthrough flags.

## KV matrix results (avg t/s over 3 reps)

| Model (class) | KV | Backend | pp@0 | pp@16384 | tg@0 | tg@16384 | VRAM |
|---|---|---|---|---|---|---|---|
| ling-3-tiny (MoE KDA/MLA) | f16 | Vulkan | 6524 | **3701** | **196** | **183** | 5.60 |
| | q8q8 | Vulkan | 5646 | **1358** | 192 | 166 | 5.55 |
| | q4q4 | Vulkan | 5739 | 1694 | 192 | 171 | 5.52 |
| | f16 | HIP | 5511 | 1963 | 108 | 103 | 5.97 |
| | q8q8 | HIP | 5459 | 1952 | 106 | 102 | 5.91 |
| | q4q4 | HIP | 5477 | 1953 | 108 | 102 | 5.88 |
| gemma-12B (dense) | f16 | Vulkan | 2125 | **1500** | **67** | **62** | 8.28 |
| | q8q8 | Vulkan | 2016 | 1064 | 64 | 58 | 7.98 |
| | q4q4 | Vulkan | 1924 | 1003 | 63 | 58 | 7.75 |
| | f16 | HIP | 1964 | 1032 | 64 | 58 | 8.60 |
| | q8q8 | HIP | 1951 | 1054 | 58 | 53 | 8.24 |
| | q4q4 | HIP | 1932 | 1062 | 55 | 50 | 8.10 |
| qwen36-a3b (`-ncmoe 32`) | f16 | Vulkan | 339 | 319 | 30 | 29 | 7.44 |
| | q8q8 | Vulkan | 340 | 299 | 30 | 29 | 7.36 |
| | q4q4 | Vulkan | 337 | 289 | 30 | 29 | 7.22 |
| | f16 | HIP | **919** | **744** | **35** | **34** | 7.78 |
| | q8q8 | HIP | 907 | 740 | 33 | 31 | 7.62 |
| | q4q4 | HIP | 910 | 737 | 33 | 27 | 7.55 |

Reading:

- **Vulkan's quantized-KV penalty is a depth phenomenon**: at depth 0 the pp gap is small; at 16384 it is −29% (gemma q8q8), −33% (gemma q4q4), −54%/−63% (ling q4q4/q8q8). RADV falls off the fused/coopmat flash-attention path when the cache is quantized. MLA is the extreme case, and **q8q8 is *worse* than q4q4 on Vulkan MLA** — kernel-path selection, not bandwidth.
- **HIP is KV-type-insensitive** — pp@16384 within ±3% across cache types on every model. Its quantized-KV fattn kernels (CUDA lineage + rocWMMA) hold the fused path.
- **No adoption case for quantized KV at ≤16k context**: VRAM saved is 0.08 GiB (MLA — the cache is already tiny), 0.53 GiB (gemma), 0.22 GiB (qwen), and tg is flat-to-worse everywhere. The break-even would need ≥64k active context on a dense model in a VRAM-constrained fit — revisit only when a real fit problem appears (ladder-v1's f16-vs-q4q4 leg covers the Vulkan long-context quality axis independently).
- **The quality leg (DeepSeek-MLA "confident gibberish" class) was not run** — its trigger (a quantized-KV config worth adopting) never fired. Note: all six quantized-MLA cells *ran without error* on both backends; correctness at depth remains unverified and stays a precondition for any future adoption plan.
- The spike-1 `-ncmoe` verdict is unchanged and extends to depth: HIP ~2.3× pp and +14% tg at 16384 on the production qwen split.

## MTP results (Qwen3.8-27B-UD-IQ3_XXS, `-c 8192`, temp 0)

| Config | tg t/s (3 prompts) | mean | draft acceptance | VRAM |
|---|---|---|---|---|
| Vulkan, MTP off | 36.5 / 36.3 / 36.1 | 36.3 | — | 11.48 |
| Vulkan, MTP on | 43.7 / 53.6 / 49.4 | **48.9 (+35%)** | 0.65 / 0.72 / 0.63 (mean len ~3.0) | 13.72 |
| HIP, MTP off | 24.1 / 23.9 / 23.9 | 24.0 | — | 12.03 |
| HIP, MTP on | 36.9 / 41.9 / 41.9 | **40.2 (+68%)** | 0.51 / 0.63 / 0.63 (mean len ~2.8) | 14.30 |

- **Capability**: `--spec-type draft-mtp` created the MTP draft context against the target model and served body-verified generations first try — no separate draft model, no extra file. The heads live in the GGUF (`blk.64.nextn.{eh_proj,enorm,hnorm,shared_head_norm}`, `qwen35.nextn_predict_layers`).
- **Lossless by construction** (target-verified speculation): per-backend output was byte-length-identical off↔on at temp 0 (±a few chars on HIP from batched-verification numerics). MTP therefore needs no quality gate — only a VRAM and throughput decision.
- **HIP gains more from MTP than Vulkan** (+68% vs +35%) — verification batches are pp-shaped work, which is HIP's stronger axis on this model — but Vulkan stays the absolute winner (48.9 vs 40.2).
- **Cost is VRAM**: ~+2.25 GiB (both backends) for the MTP draft context at `-c 8192`; expect it to scale with context.
- **Model availability is the constraint**: the unsloth Qwen3.8-27B quants are the *only* local GGUFs carrying MTP heads. *(Corrected 2026-08-25: the spike originally attributed Ling-3.0-tiny's missing heads to the bloomer010 conversion — wrong. Upstream `inclusionAI/Ling-3.0-tiny` itself ships `num_nextn_predict_layers: 0`; the tiny checkpoint never had MTP heads, so no conversion can provide them. Larger Ling siblings do carry them.)* Availability is per-checkpoint *and* per-conversion — e.g. unsloth publishes Qwen3.6-35B-A3B in two repos, plain and `-MTP-GGUF` (heads preserved); our on-disk qwen36-a3b files are the plain variant. Check before downloading any MoE/MTP-family GGUF (30-second header scan: `head -c 32M file.gguf | strings | grep nextn`).

## Stability

Zero occurrences of the tightened signature family (`\bMES\b`, `Failed to evict`, `GPU reset`, `wedged`) in `journalctl -k` across the window (18 bench cells + 4 server configs), checked between every cell and at close; watchdog `active` throughout, never exercised; post-window `smoke-inference.sh` all green. Two footnotes:

- One benign advisory during HIP `-ncmoe` cells: `workqueue: svm_range_deferred_list_work [amdgpu] hogged CPU for >10000us` — KFD shared-virtual-memory bookkeeping under heavy host↔device traffic; an advisory, not a hang precursor. (It also exposed a monitoring lesson: case-insensitive `MES` grep matches the word "times" — use `\bMES\b` case-sensitive.)
- The `HSA_OVERRIDE_GFX_VERSION` ban stands absolute; both drivers refuse to run with it set.

## Recipe dimensions for bakeoff

The end goal (user, 2026-08-24): these factors become bakeoff cell dimensions — per-model serving-recipe optimization on given hardware. Mapping, per dimension:

| Dimension | Flags | Measured effect | Bakeoff status |
|---|---|---|---|
| KV cache type | `-ctk/-ctv` (server: same) | f16 wins at ≤16k ctx; quantized costs Vulkan pp at depth (−29…−63%), saves ≤0.53 GiB | **Available today** (flag-level, Vulkan queues) — but only worth cells in a VRAM-constrained ≥64k-context fit study; pair any adoption candidate with a correctness leg (MLA gibberish class) |
| Flash attention | `-fa on` | Precondition, not a dimension — quantized V requires FA; all cells pin `-fa on` | Pin in cell `server_flags`, don't sweep |
| MTP | `--spec-type draft-mtp` | Lossless +35% tg (Vulkan) on Qwen3.8-27B; +2.25 GiB; needs nextn tensors in the GGUF | **Available today** (flag-level) for MTP-capable models; also a *model-selection* criterion — scan headers for nextn before downloading a conversion |
| Backend (Vulkan/HIP) | container image | HIP wins `-ncmoe` (2.3× pp @ depth) and is KV-type-insensitive; Vulkan wins the rest | **Blocked on the dual-backend image** (parked plan — spike-1 findings recommendation 2); single container / single swap domain when it lands, never a second GPU container |

Concrete recommendation for the next queue design: no KV-type sweep (settled negative at current context sizes); add an MTP on/off pair for any MTP-capable model under bake-off; keep `-fa on` pinned; revisit the backend dimension only behind the dual-backend image plan.

## Recommendations

1. **Production stays Vulkan + f16 KV** (contract + now measured twice). Nothing in this spike changes a production alias.
2. **MTP is the first recipe lever worth a production experiment** when an MTP-capable model earns a serving slot (today: Qwen3.8-27B, not currently an alias; its 3.1-bpw quant + MTP context fits in ~13.7 GiB). Any such experiment is its own plan (alias-contract care, VRAM fit at real context).
3. **Header-scan new GGUFs for nextn tensors before download decisions** — availability differs per checkpoint (Ling-3.0-tiny has none upstream) and per conversion (unsloth ships separate `-MTP-GGUF` repos), and the delta is a free +35% decode.
4. Re-evaluate triggers unchanged from spike 1 (Qwen3.8-class `-ncmoe` MoE → HIP experiment; vLLM native gfx1201 kernels → gateway leg).

## Addendum 2026-08-25 — qwen36-a3b MTP measured (post-spike follow-up)

Downloaded `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` UD-Q4_K_XL (21.3 GiB; separate repo from the plain files production serves — heads verified `blk.40.nextn.*` by header scan before and after download) and ran the same `mtp-ab.sh` A/B, **Vulkan only, `-ncmoe 32`** (production split), `-c 8192`:

| Config | tg t/s (3 prompts) | mean | acceptance | VRAM |
|---|---|---|---|---|
| MTP off | 29.0 / 29.1 / 28.9 | 29.0 | — | 7.49 GiB |
| MTP on | 44.6 / 47.8 / 50.5 | **47.6 (+64%)** | 0.65 / 0.74 / 0.78, mean len ~3.2 | 8.66 GiB |

- **The amortization hypothesis held**: on the CPU-offloaded-expert split, decode pays expert streaming per token; MTP verification batches ~3 tokens per pass, so the relative gain (+64%) exceeds the dense 27B's (+35%) — and the VRAM cost is smaller (+1.17 GiB vs +2.25).
- **This beats the HIP option outright**: HIP bought +17% tg on this model (35.0 vs 29.9) and requires the dual-backend image; Vulkan+MTP buys +64% with zero image work. The dual-backend image case just lost its only production-relevant payoff on the tg axis (HIP's pp advantage stands).
- Adoption is **not** a config tweak: the MTP variant is a *different backing file* than the live `qwen36-a3b` alias — per the alias contract (and the gemma-qat precedent in `llama/config.yaml`), that's a new-alias-or-deliberate-repoint decision with output-equivalence verification, i.e. its own small plan. Depth behavior (acceptance at long context) also unmeasured here.
- Second candidate noted: unsloth's gemma QAT repo ships `mtp-*` sidecar files (config.yaml comment records they were deliberately skipped at alias creation).

Raw results: `runs/rocm-spike/results/kv-*`, `mtp-*` (scratch); snapshot + WP reports archived in archive-repo under `inference-engine/gfx1201-kv-fa-mtp-spike/`.

## Addendum 2026-08-25 — MTP rollout executed (plans/mtp-serving-rollout.md)

The rollout landed the same day, with user-directed scope amendments (rev 2–3: Ling quant reduction; qwen36 held to Q4 only, Q5/Q6 MTP downloads cancelled). Serving state after WP-2/WP-3, all verified live by response body:

| Alias | Change | Fit at real `-c` (baseline 0.76 GiB desktop) | MTP engagement |
|---|---|---|---|
| `gemma-4-12b-qat` | +`--mmproj` (vision) +`-md` sidecar +`--spec-type draft-mtp`, `-c 32768` | 9.73 GiB used → **≈6.2 GiB free** (stable across chat + vision probes) | draft_n 36/29 accepted (0.81), 81.9 tok/s |
| `qwen36-a3b` | `-m` → MTP-GGUF variant +`--spec-type draft-mtp`, `-ncmoe 32`, `-c 100000` | 10.74 GiB used peak → **≈5.2 GiB free**; +16 MB only across a 4,992-token needle probe (pre-allocated) | acceptance 0.85/0.83, tg 54.3–54.6 tok/s (vs ≈29–30 pre-MTP: **+~85% at short generations**) |
| `gemma-4-12b` | REMOVED (user-directed; consumers repoint to `gemma-4-12b-qat`, tracked in INITIATIVE.md) | — | — |
| `ling-3-tiny-q4-m`, `qwen36-a3b-q5`, `qwen36-a3b-q6` | REMOVED (quant reductions; no consumers) | — | — |

Neither fit gate tripped — no `-c` step-down anywhere; the draft-context-scales-with-`-c` concern was real but small at these contexts (qwen's whole MTP+KV+buffer overhead at `-c 100000` is ~4.5 GiB above weights-resident, dominated by pre-allocated KV, not the draft context). Ladder-v1 was retired terminal (`completed` + retirement annotation — batch.py has no cancelled state; 2 finished gemma cells retained); the plain qwen dir (76 GiB), the lmstudio gemma dir (7.1 GiB), and the Ling Q4_K_M file (4.5 GiB) were deleted (~88 GiB reclaimed), and a post-deletion probe confirmed serving reads only the MTP paths. A future ladder-v2 measures the MTP recipes.

**Dual-backend image: retired as a queued item entirely** (user decision 2026-08-25, superseding recommendation 4's HIP re-evaluate trigger): decode is the limiter for most real workflows, Vulkan+MTP owns decode, and HIP's remaining pp advantage does not justify the second load-bearing pin, the silent-alias failure class, and the rebuild coupling documented in the spike. HIP numbers above stay as measurement record only.

## Addendum 2026-08-25 — long-context serving defaults landed (plans/long-context-serving-default.md)

Goal (user): 128k+ (`-c ≥ 131072`) as the serving default on every chat alias where ceiling + VRAM permit, with 262144 live-tested per alias for valid configuration (collapse check). All probes: needle-at-depth at ~89–90% real prompt fill (`usage.prompt_tokens`, never assumed), temp 0, trace-read classification; fit gate ≥~1.5 GiB free at peak; card = 15.92 GiB nameplate, ~0.85 GB desktop baseline.

| Alias | Landed default | Free VRAM @default (peak) | 256k verdict | Notes |
|---|---|---|---|---|
| `nemotron-4b` | 32768 → **131072** | >9 GB | **VALID** (exact recall @234,542 real tokens, >7 GB free, pp 1,649 t/s) | Backing file upgraded lmstudio Q4_K_M → unsloth UD-Q4_K_XL (user-directed same-alias upgrade). Header claims 1M ctx in BOTH conversions; clamped to NVIDIA base config's 262144. Hybrid mamba: KV 16 KiB/token (4/42 attn layers). Routinely answers in `reasoning_content` with empty `content` on terse asks. |
| `qwen36-a3b` | 100000 → **131072** + `-ub 2048` | 3.46 GiB | valid-but-UNFIT (exact recall @234,971, MTP acceptance 0.87, no collapse — but 1.05 GiB free solo, ~175 MB with bge-m3 co-resident; interp. max passing ≈240k) | **`-ub 2048` expert-streaming amortization confirmed** (user-directed experiment, plan rev 2): pp 321.5→745.4 t/s @6.5k (2.32×), 270.6→600.0 @118k (2.22×; 7.3→3.3 min), +1.0 GB compute buffer, decode unchanged. Mechanism: `-ncmoe` prefill streams expert weights over PCIe per micro-batch; 4× `-ub` = ¼ the transfers. Dense/GPU-resident models: nothing to amortize. |
| `gemma-4-12b-qat` | 32768 → **131072** (vision + MTP resident) | 4.4 GiB | **VALID AND FITS** (exact recall @234,066, acceptance 0.79, 1.87 GiB free solo — the predicted ≈8 GiB-KV f16 fit-fail did NOT materialize; SWA-aware allocation is lighter) | 262144 is user-selectable; co-residency margin (~0.7 GB with bge-m3) kept the default at 131072. Vision re-verified at landed config (red PNG → "Red"). pp 897 t/s @118k. |
| `ling-3-tiny-*` ×3 | 131072 (unchanged) | — | impossible (hard header ceiling 131072, `rope_scaling: null`) | Documented exception, no change. |
| `qwen38-27b-iq3xxs` | 40960 f16 → **131072 q4q4+fa** | 2.50 GiB | fit-FAIL, live-measured (262144 loads with ~142 MB free; reverted) | q4q4 ladder propagation (user choice): IQ3_S's proven recipe → all three quants; fused-fa confirmed by VRAM arithmetic per quant. |
| `qwen38-27b-iq3s` | 120000 → **131072 q4q4+fa** | 1.59 GiB | no-fit (strictly worse than XXS) | Gate holds, barely. |
| `qwen38-27b-q3kxl` | 24576 f16 → **65536 q4q4+fa** | 1.73 GiB | no-fit | Max-safe rung: 81920 predicts ≈1.48 GiB (under gate); 131072 analytically impossible at 12.2 GiB weights — the plan's expected step-down, with numbers. |

Cross-cutting findings:

- **Co-residency is the real 256k blocker, not the solo gate.** Crucible's `bge-m3` (its own `-ub 8192` buffers) spawns beside whatever chat model is resident; during qwen36's 256k window it took the card to 16.92/17.10 GB. A 256k default would starve the embed family that co-runs in practice — this, not raw fit, is why gemma's *passing* 256k config stays non-default.
- **MTP draft-context scaling to 262144 is a non-issue**: qwen36 and gemma both engaged MTP at ~234k real tokens with acceptance 0.79–0.87 — the draft context grows sub-linearly in the total footprint and never tripped anything on its own.
- **256k prefill walls (measured)**: nemotron 142 s; gemma 6.8 min; qwen36 16.9 min at `-ub 512` (the `-ub 2048` gain would roughly halve it, landed config applies at ≤131072).
- Candidate follow-ups (not planned): KV slot save/restore (`--slot-save-path` + `/slots` API) for pausable long-context agent sessions — tiering via page cache/tmpfs, restore ≈1–2 s vs 3–17 min re-prefill; caveats to verify: geometry lock, MTP draft state coverage, hybrid-mamba (nemotron) session-save support. `-ub` sweep beyond 2048 for the `-ncmoe` split.

Verification recipe unchanged from the rollout: probe by response body; count `content + reasoning_content` before calling a recall wrong; fused-fa via VRAM arithmetic (llama-swap never surfaces child stdout). ladder-v2 (`llama/bakeoff/queues/ladder-v2.json`, 11 cells over the landed recipes incl. both 256k-valid points, needle+timing batteries) was deleted un-run by `plans/overnight-test-matrix-redesign.md` WP-3 — superseded by the `matrix-v1-shared`/`matrix-v1-dedicated` pilot matrix queues (`llama/bakeoff/queues/`, see that README's "Test matrix" section); its aliases retire unused but stay reserved under the append-only alias contract.
