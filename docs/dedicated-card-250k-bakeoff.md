# Dedicated-card 250K bake-off: serving engines and long-context throughput on the RX 9070

Fleet plan `dedicated-card-250k-agent-bakeoff` (rev 2, IMPLEMENTED 2026-11-25). Card: RX 9070 (gfx1201), 16,304 MiB total (15.922 GiB, the nvtop view), idle baseline 57–103 MiB, so at most 16,201 MiB for the model process. Every GPU action ran inside the `cron.lock` bake-off window with the live `inference-engine-llama` stopped, then restarted and smoked green; the router and codex-sidecar were never touched. 11 windows, 0 GPU hangs, every window `smoke PASS`. Raw evidence stays in the gitignored spike scratch directory.

## What this is (and is not)

A measurement of **which serving engines and placements carry a 262144-token context on this card, and how decode and prefill degrade with depth**, with the card dedicated to one model. Qwen3.6-35B-A3B and Gemma-4-26B-A4B were the reference workloads that exercised the engines, the depth harness and the quality harness. **They are not serving candidates:** neither is capable enough to stand in for a Claude model (user, 2026-11-25). This doc therefore makes **no serving recommendation**. The models that are smart enough — Inkling 2 Mini, Qwen3.8-27B, Qwen3.8 Flash Next — are the follow-on (see "What comes next").

## Engines and harness

| Kind | Image | Notes |
| --- | --- | --- |
| `vk10534` | `crucible-llamacpp-vulkan:b10534-swap243` | the served build |
| `vk12692` | `inference-engine-llama:spike-b12692` | current mainline Vulkan (needed for `inkling2_loop`) |
| `hipg` / `hipn` | `inkling-spike/unsloth-rocm:b12649-mix-graphs` / `-nographs` | matched pair, identical source, only `-DGGML_HIP_GRAPHS=ON/OFF` (CMakeCache-proven) |
| Strata | native engines (`strata`, `strata-inkling`) | rows tagged `native`, kept separate |

Depth harness (`depth2.pl`): tokenizer-sized strict-extension prompts (instruction first, so each depth extends the last), `ignore_eos`, 256 generated tokens, thinking off; depth set 2k / 32k / 131k / 200k / 250k; rows labelled by **actual** prompt tokens (sizing error ≤ 1%). A 262k curve is `complete` only with points within ±10% of 32k/131k/200k and one ≥ 245k. The guard samples VRAM throughout: over `CAP_MIB` 15,667 → `OVERCAP`; GTT growth over the slack → `GTT-SPILL`.

## Throughput table (all at `-c 262144`, q8_0/q8_0 KV unless noted; tg / pp in tok/s at the actual depth)

| Config | Placement | Self peak MiB | tg 2k | tg 32k | tg 131k | tg 250k | pp 131k | pp 250k | Replay min | Curve |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Q36 · hipg · MTP | `-ncmoe 28` | 15,544 | 66.6 | 57.5 | 45.2 | **34.9** | 1,032 | 544 | **20.6** | complete |
| Gemma · hipg · MTP | `-ncmoe 16` | 15,429 | 70.3 | 53.7 | 40.5 | 28.3 | 675 | 272 | 23.8 | complete |
| Gemma · vk12692 · MTP | `-ncmoe 8` | 15,698 | 73.4 | 64.7 | 44.9 | – | 993 | – | – | OVERCAP at 200k |
| Gemma · vk12692 · no MTP | `-ncmoe 4` | 15,822 | 67.0 | 57.7 | – | – | – | – | – | OVERCAP at 131k |
| Q36 · vk12692 · MTP | `-ncmoe 24` | 15,688 | 49.2 | 50.2 | 35.7 | – | 590 | – | – | OVERCAP at 200k |
| Q36 · vk10534 · MTP | `-ncmoe 24` | 15,767 | 52.8 | 48.2 | 41.2 | – | 470 | – | – | OVERCAP at 200k |
| Q36 · hipg · MTP · f16 KV | `-ncmoe 32` | 15,674 | 59.8 | – | – | – | – | – | – | OVERCAP at 32k |
| Inkling Q2_K_XL · hipg | 8 single-pass blocks' experts on CPU | 15,675 | 40.6 | – | – | – | – | – | – | OVERCAP before 32k |
| Inkling Q2_K_XL · hipn | same | 15,674 | 37.2 | – | – | – | – | – | – | OVERCAP before 32k |
| Inkling IQ3_XXS · hipg | 12 blocks on CPU | 15,768 | 34.5 | 31.7 | – | – | – | – | – | OVERCAP before 131k |
| Inkling Q4_K_XL · hipg | 16 blocks on CPU | 15,810 | 23.3 | 22.3 | – | – | – | – | – | OVERCAP before 131k |
| Inkling Q4_K_XL · hipn | same | 15,810 | 21.8 | 20.5 | – | – | – | – | – | OVERCAP before 131k |
| Inkling Q2/Q4 · vk12692 (q8/q8 and q8/q4 KV) | all steps | – | – | – | – | – | – | – | – | NOFIT (GTT-SPILL / OVERCAP / OOM) |
| Inkling Q2 · hipg resident (`-ngl 99`) | none | – | – | – | – | – | – | – | – | OOM at load (262k) |
| Qwen3.8-27B IQ3_XXS · vk12692 | `-ngl 99`, q4_0/q4_0 KV, `-ub 512` | 14,958 | 29.0 | not run (one-shot by plan) | | | | | | LOADED |

- Replay = `replay.pl` over the recorded agent transcript `tx-ed320669.tsv` at each config's curve (only complete curves rank; truncated curves carry 27 overflowed calls).
- Run-to-run noise: three Q36·hipg runs of the 131k point gave 45.2 / 47.6 / 49.4 (MTP draft acceptance 0.64–0.68); 2k and 32k matched within 1%. Treat differences under ~10% at depth as noise.
- **Vulkan GTT-slack deviation:** with CPU expert offload, Vulkan imports host weights as GTT and grows GTT by more than 1 GiB at load on *every* placement, including `-ncmoe 40` at 7.7 GB VRAM. The ranked sweep therefore used a 4,096 MiB GTT slack for `vk*` kinds (HIP kept 1,024); the 1,024-slack verdicts are kept as `@gtt1024` NOFIT rows. Accepted-config growth during depth was +1,228 / +166 MiB, far below the +5,154 MiB real-spill signature, and `CAP_MIB` was never loosened.
- **Vulkan's ceiling is depth growth, not load fit:** every Vulkan arm loads under the cap and then crosses it as context fills (Q36 at 200k, Gemma at 131–200k). The HIP build's footprint grows ~84 MiB from load to 250k (Q36: 15,460 → 15,544).

### Strata native rows (`strata.tsv`, tagged `native`)

| Model | N | G (expert GiB on GPU) | pp | tg | Self MiB |
| --- | --- | --- | --- | --- | --- |
| Inkling Q2_K_XL | 32,768 | 10 | 829 | 33.8 | 14,913 |
| Inkling Q2_K_XL | 131,072 | 8 | 455 | 16.7 | 14,404 |
| Inkling Q2_K_XL | 250,000 | 6 | 289 | **10.6** | 14,216 |
| Inkling Q4_K_XL | 32,768 | 10 | 672 | 25.8 | 14,952 |
| Inkling Q4_K_XL | 131,072 | 8 | 472 | 14.9 | 14,446 |
| Inkling Q4_K_XL | 250,000 | 6 | 350 | **9.7** | 14,256 |
| Q36 Q4_K_XL | 32,768 | 6 | 3,141 | 41.6 | 14,650 |

Strata is the only engine that carries Inkling to 250k on this card; Q36 under Strata did not reach OK at 131k or 250k (G = 8/6/4 OVERCAP; the 250k G=8 row is a run failure, not OVERCAP). Inkling rows at a larger G that overran the cap are recorded `CLAMPED`/OVERCAP, not retried.

## Inkling: graphs on vs off, and why it is slow

- **HIP graphs are not the bottleneck.** Matched builds: Q2 40.6 (on) vs 37.2 (off) tg at 2k; Q4 23.3 vs 21.8 — ~7–9%. Launch overhead is not where the time goes.
- **The card is saturated while Inkling crawls.** During Inkling depth runs on HIP the GPU sits at 95–98% busy, 208–217 W of the 220 W cap, 2.79 GHz sclk — while decoding 22–41 tok/s. Inkling 2 Mini (`128x1.8B`, 8 of 128 experts, expert FFN 1152, `n_embd` 2304) runs blocks 6–17 **three times** (`loop.count 3`), i.e. 48 layer evaluations per token; even so the per-token weight traffic is on the order of 1–2 GB at Q2, a bandwidth ceiling several hundred tok/s away. Being power- and busy-saturated ~10× under the bandwidth roofline means a compute- or kernel-path problem, not memory.
- **llama-server memory grows with depth** on Inkling (OVERCAP before 32k at Q2 with 8 blocks on CPU, although the KV cache itself is small: 4 full-attention layers × 8 KV heads × 128, the rest SWA-1024 → ~2 GiB at 250k). Something besides the KV cache scales with context.
- Inkling-specific ops that are candidates for a slow or unfused path: relative attention (`relative_length 16 / extent 1024`), QK clip, log-scale attention, `attn_res` block residuals, shortconv, loop gates, softcapping; and the MoE shape itself (expert FFN 1152 is not a multiple of the 256-element K-quant super-block). These are hypotheses for the follow-on, not findings.

## Card headroom (user direction 2026-11-25: "how close can we run to the card's total VRAM")

These rows never enter the ranked tables (`headroom.tsv`, `results.headroom.tsv`).

| Step | Placement | Cap MiB | Peak self | Peak total (nvtop) | Result |
| --- | --- | --- | --- | --- | --- |
| Q36 · hipg accepted | `-ncmoe 28` | 15,667 | 15,544 | 15,601 (95.7%) | 250k, tg 34.9 |
| Q36 · hipg `hr-hipg-s1c` | `-ncmoe 27` | 16,201 | 15,951 | **16,008 (98.2%, 15.63 GiB)** | 250k, tg 34.4, pp 624 |
| Q36 · hipg `hr-hipg-s1b` | `-ncmoe 26` | 16,201 | 16,215 | 16,272 | OVERCAP at load |
| Q36 · vk10534 `hr-vk10534-s1` | `-ncmoe 22` | 15,872 | 16,160 | 16,217 | OVERCAP at load (GTT +3,138) |

The card can run at ~98% of its total VRAM through 250k on HIP, but the extra expert layer buys nothing measurable (34.4 vs 34.9 tg at 250k, within noise). The worker's first probe coupled each placement step to the lowest raised cap (both engines broke at load); the orchestrator re-probe above decoupled them.

## GPU utilisation ("can we fill the GPU, or is temp/power limiting it?")

Joined from the 1 s sampler (`gpu-util.tsv`) to each config's guard time range (first 30 s excluded); depth runs unless marked `-q` (quality battery).

| Config | Busy % | Power W (cap 220) | sclk MHz |
| --- | --- | --- | --- |
| Q36 · hipg (verify runs) | 95.5–98.9 | 195–201 | 2,480–2,600 |
| Q36 · hipg · quality battery | 96.5 | 200 | 2,570 |
| Gemma · hipg · MTP (depth / battery) | 97.6 / 98.3 | 213 / 215 | 2,800 |
| Inkling Q2 / IQ3 / Q4 · hipg/hipn | 95.4–98.1 | 208–217 | 2,790 |
| Q36 · vk12692 | 68.2 | 94 | 1,474 |
| Gemma · vk12692 · MTP | 78.0 | 135 | 1,868 |

On HIP the card is effectively at its power limit (busy % can include spin-wait, but the power draw is real); on Vulkan it idles 22–32% of the time at half the power and lower clocks — the 60–100% spikes seen in nvtop were Vulkan with CPU expert offload (CPU-side expert stalls + batch-1 sync). Not thermal: edge 53–54 °C / junction 58–59 °C in the spot sample. The Vulkan→HIP gap is the "fill the GPU" opportunity for offloaded MoE; for Inkling the GPU is already full and the problem is what it is doing.

## Quality harness (calibration of the gate, on the reference workloads)

Battery: `qual.py` over the eval-harness live battery (single needle, multi-needle ordered readback, passage QA; positions 0.10 / 0.45 / 0.85), tokenizer-calibrated, thinking off (`chat_template_kwargs.enable_thinking=false`, verified effective on Q36 and on Gemma's template), caps 256 / 768 / 512; `toolprobe.sh` ×3; 120 min GPU per arm.

**Gate aggregation rule (pinned):** a cell = task × position; `unmeasured` trials (observed prompt outside ±5% of target, prompt + cap > ctx, transport) are excluded and a cell with < 2 measured trials is `unmeasured`; a cell passes at ≥ 2/3 of its measured trials `pass` (`truncated` counts as not-pass, reported separately); a context passes when every single_needle cell passes and ≥ 2 of 3 cells pass for multi_needle and for passage_qa; toolprobe passes at 3/3 runs scoring 1/1/1; overall = gate@250k AND toolprobe.

| Arm | gate@250k | gate@128k | toolprobe | Trials (pass / fail / trunc / unmeas) | Agent trial |
| --- | --- | --- | --- | --- | --- |
| Q36 · hipg `-ncmoe 28` | PASS | FAIL (2 passage_qa cells unmeasured — cap) | PASS | 33 / 0 / 0 / 1 | pass — 24 s, 20 turns, peak ctx 16,221 |
| Gemma · hipg · MTP | FAIL | not run (cap) | PASS | 7 / 6 / 1 / 0 | pass (labelled gate-failed) — 62 s, 11 turns |
| Gemma · hipg · no MTP | FAIL | not run (cap) | PASS | 8 / 5 / 1 / 0 | not run |

Raw cells: the spike's `gate.txt` (`gate.pl`). Coverage was capped, not designed: a 250k prompt costs ~5 min on Q36 and 9–13 min on Gemma, so no arm reached a third round and most cells hold 1–2 trials. Gemma's failures are real wrong answers, not missing samples. Orchestrator re-run of one Q36 250k multi_needle trial: same verdict (pass, 320 s vs 307 s). The agent trial (`claude -p` → llama-server `/v1/messages` at :15899 on a fixed 5-test fix-up task in a throwaway seed) proves the endpoint path works end to end on the HIP build; it is a smoke of the path, not a capability verdict.

## Candidate geometry for the follow-on (header-read, not measured here)

| Model | Attention | KV at 250k | Weights | Prior long-context data |
| --- | --- | --- | --- | --- |
| Inkling 2 Mini | 4 of 24 layers full (8 KV × 128), rest SWA 1024; blocks 6–17 looped ×3 | ~2 GiB | Q2 8.5 GiB / IQ3 10.9 / Q4 16.4 | this doc (above); Strata 250k at ~10 tg |
| Qwen3.8-27B (dense, hybrid SSM) | 16 of 65 full (4 KV × 256) | q8 8.3 GiB / q4_0 4.4 GiB | IQ3_XXS 10.2 GiB / IQ3_S 11.2 | served at `-c 131072` q4_0 KV: 98–128k prompts answered, decode 26–36 tg, prefill ~377–763 t/s, needle 27/27 (`docs/test-matrix-results.md`, `docs/qwen38-27b-bakeoff-results.md`); 262k loads all-GPU at q4_0 (14,958 MiB, this doc); no depth curve above 131k |
| Qwen3.8 Flash Next (`qwen4exp`, 512 experts / 10 used) | 12 of 48 full (2 KV × 256) + sparse indexer | ~3 GiB (q8) | Q2_K_XL 73.5 GiB; PLE/n-gram tables stream disk → RAM on demand (`-lm mmap`) | measured to 16k only: 13–17 tg, 9/9 needle (`docs/qwen4exp-bakeoff-results.md`); slowdown far milder than a 180B-class model's size suggests |
| Nanbeige4.3-33B (dense Llama) | all 52 full (8 KV × 128) | q8 26 GiB / q4_0 14 GiB | IQ3_XXS 14.5 GB | **excluded** — the KV cache alone exceeds the card at 250k; not worth testing (user, 2026-11-25) |

Both current builds (`vk12692`, `hipg`) contain the `qwen4exp` and `nanbeige` architectures.

## What comes next

1. **Inkling resident-path performance (priority).** Why does a GPU-resident, low-active-compute looped MoE decode at 22–41 tok/s with the card power-saturated, and why does llama-server's footprint grow with depth? Profile per-op time on HIP (rocprof / `GGML_SCHED` debug / `test-backend-ops perf` on Inkling's shapes — MUL_MAT_ID with expert FFN 1152, the relative-attention and loop-gate ops), compare against Strata's kernels, and find the bug or the fusion opportunity.
2. **Qwen3.8-27B to 250k:** full depth curve (q4_0 KV, `vk12692` and `hipg`, `-ub` sized to the margin) and the quality gate at 128k/250k — KV q4_0 at 250k is the quality risk, prefill (~314–380 t/s at `-ub 512`) the agent-latency risk.
3. **Flash Next beyond 16k:** the KV geometry supports 250k cheaply; the open question is throughput at depth with PLE streaming.
4. "Fill the GPU" for offloaded MoE: the Vulkan vs HIP utilisation gap above.

## Residual caveats

- Spike images only (`spike-b12692`, `b12649-mix-graphs/-nographs`); nothing here is promoted or served.
- Quality cells hold 1–2 trials (120-min cap); gate@128k not completed for any arm.
- Decode at depth varies ~±5% run to run (MTP acceptance); MTP rows depend on it.
- The Vulkan GTT slack (4,096 MiB) is a measured classifier change for `vk*` kinds only, recorded above.
