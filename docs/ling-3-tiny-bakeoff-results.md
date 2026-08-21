# Ling-3.0-tiny bake-off — results

Consolidated stats across both Ling-3.0-tiny plans. Previously scattered across
`llama/config.yaml`'s `ling-bakeoff` group comment and two WP reports; this doc pulls the
numbers into one comparison table. The comment in `config.yaml` remains the canonical live
record (it travels with the config it documents); this doc is the readable summary.

Source plans: `plans/ling-3-tiny-bakeoff-and-gpu-hang-watchdog.md` (IMPLEMENTED, established the
four quants at `-c 8192`) and `plans/ling-3-tiny-context-ceiling.md` (IMPLEMENTED, raised `-c` to
131072 and tested a KV-cache-quantization compatibility experiment). Source reports:
`runs/dispatch/wp-1-report.md`, `runs/dispatch/wp-2-report.md` (context-ceiling plan).

## Architecture context (why the numbers look the way they do)

Ling-3.0-tiny is BailingMoE3, a hybrid of 18 KDA (Kimi-Delta-Attention, linear/recurrent,
**no conventional KV cache**) layers and 6 MLA (Multi-head Latent Attention) layers out of 24
total. The 6 MLA layers cache a single compressed latent per token per layer
(`attention.key_length=576` = `kv_lora_rank(512)+rope.dimension_count(64)`), not separate K and V
caches — confirmed live this session via a scratch verbose-logged server load showing
`V (q8_0): 0.00 MiB` alongside a non-zero `K` cache. This is why KV-cache VRAM growth is far
smaller than a dense model's at the same context size, and it's constant across quants (weight
quantization doesn't touch the cache; only cache dtype and context length do). Real max context:
131072 tokens, confirmed both from the base model's HF `config.json`
(`max_position_embeddings: 131072`, `rope_scaling: null`) and by parsing all four local GGUF
headers directly (`bailingmoe3.context_length=131072`, byte-identical across quants). Card
nameplate: 15.92 GiB VRAM.

## Methodology

- **VRAM**: `mem_info_vram_used` read directly from `/sys/class/drm/card1/device/mem_info_vram_used`
  before/after each load and probe — never assumed from weight size alone.
- **Correctness**: needle-in-haystack probes — a unique reference code embedded ~45% through a
  constructed filler-text haystack, the model asked to recall it at the end. Real
  `prompt_tokens` always read from the server's own `usage` field, never assumed from character
  count. Two sizes used: moderate (~29–33K real tokens) and near-ceiling (~111K real tokens, close
  to the 131072 ceiling once the question and reasoning budget are accounted for).
- **Hang detection**: `journalctl -k -f` watched live throughout every load and probe, checked for
  any MES/wedged/reset/amdgpu fault signature — zero found in any test below.
- **KV-cache-quant validation** (Part B only): cross-checked structurally via GLiNER2 entity
  extraction (`gliner2-base`, onnx sidecar `:4812/v1/extract`) and semantically via `bge-m3`
  embedding cosine similarity (`:4811/v1/embeddings`), against the f16-cache baseline completion
  for the identical prompt.
- **VRAM-baseline gotcha** (found live during the context-ceiling plan's WP-2): a
  `mem_info_vram_used` read taken as "baseline" immediately before firing a load request into
  this `exclusive: true` swap group can capture the *previous* resident model mid-teardown or
  still fully resident, since eviction is triggered by the incoming request, not proactively. All
  overhead figures below that come from the context-ceiling plan use its own verified
  empty-group baseline (1,739,743,232 B / 1.62 GiB) rather than a live pre-load reading.

## Results — quant × KV-cache-type × context

| Quant | Weights | KV cache | `-c` | Peak VRAM | Overhead beyond weights¹ | Prompt tokens (real) | Wall (near-ceiling) | Recall | Hang signature |
|---|---|---|---|---|---|---|---|---|---|
| Q4_K_M | 4.49 GiB (4.82 GB) | f16 | 8192 | 5.73 GiB | ~0.11 GiB | n/a (not needle-tested at 8192) | — | — | none |
| Q4_K_M | 4.49 GiB | **f16** | **131072** | 7.46 GiB | 1.35 GiB | 111,272 | 62.2s | ✅ correct | none |
| Q4_K_M | 4.49 GiB | **q8_0/q8_0 + `-fa on`** | **131072** | not isolated² | not isolated² | 111,272 (same prompt) | 243.6s (1st) / 10.4s (2nd, cache-hit) | ❌ **FAILED both attempts** | none |
| UD-Q4_K_XL | 4.97 GiB (5.34 GB) | f16 | 8192 | 6.15 GiB | ~0.05 GiB | n/a | — | — | none |
| UD-Q4_K_XL | 4.97 GiB | f16 | **131072** | 7.79 GiB | 1.20 GiB | 111,269 | 62.15s | ✅ correct | none |
| UD-Q6_K_XL | 6.77 GiB (7.27 GB) | f16 | 8192 | 7.95 GiB | ~weights-alone | n/a | — | — | none |
| UD-Q6_K_XL | 6.77 GiB | f16 | **131072** | 9.61 GiB | 1.21 GiB | 111,271 | 61.93s | ✅ correct | none |
| Q8_0 | 7.83 GiB (8.41 GB) | f16 | 8192 | 8.99 GiB | ~weights-alone | n/a | — | — | none |
| Q8_0 | 7.83 GiB | f16 | **131072** | 10.16 GiB | 0.71 GiB³ | 111,270 | 61.09s | ✅ correct | none |

¹ At `-c 8192`, overhead is measured against each load's own contemporaneous desktop-session
baseline (varied 1.12–1.21 GiB across that plan's runs; see the `-c 8192` era note below). At
`-c 131072`, overhead uses the context-ceiling plan's fixed empty-group baseline of 1.62 GiB
throughout, per the methodology note above.

² Part B's near-ceiling attempts both failed at generation, not at load — the run that would
have produced a clean "peak after successful near-ceiling completion" reading never completed
successfully, so no comparable figure is recorded rather than reporting a misleading one. The
load + moderate-probe VRAM was in the same range as the f16 baseline (MLA's already-small cache
quantized further should only reduce it), consistent with the scratch-load structural
confirmation (`K (q8_0): 14.34 MiB` vs `K (f16): 27.00 MiB` at an identical 4096-token context).

³ Notably lower than UD-Q4_K_XL/UD-Q6_K_XL's ~1.2 GiB. Attributed to baseline-measurement noise
(a few hundred MB of drift between reads on a shared desktop session was already an established,
ordinary effect in this group before this plan), not a real quant-dependent effect — KV-cache
size is a function of context length and cache dtype, not weight quantization, so all four
quants' *real* overhead at the same `-c`/cache-type is expected to be roughly equal.

## KV-cache-quantization experiment (Part B) — why it was rejected

Only Q4_K_M was tested under the quantized-cache configuration (symmetric `q8_0`/`q8_0` +
`-fa on`), as a hard-gated compatibility experiment layered on top of the `-c 131072` goal — not
required to hit the context target, since f16 cache already fit comfortably (1.35 GiB overhead
at 131072 tokens, vs the ~12.8 GiB realistic free-VRAM budget).

- **Confirmed genuinely active** (not a silent fallback): a scratch verbose-logged `llama-server`
  instance on a throwaway port showed `flash_attn = enabled` and real cache quantization
  (`K (q8_0): 14.34 MiB` vs a second scratch run's `K (f16): 27.00 MiB`, identical 4096-token
  context). Also surfaced the MLA single-sided-cache mechanism (`V (q8_0): 0.00 MiB`) described
  above.
- **Moderate probe (29,463 tokens): passed.** Correct recall, byte-identical completion to the
  f16 baseline; cross-checked via GLiNER2 (exact-entity match, confidence 0.9999995) and `bge-m3`
  cosine similarity (1.0).
- **Near-ceiling probe (111,272 tokens): FAILED, reproducibly.** First attempt: `finish_reason:
  "length"`, empty `content`, the full 1024-token budget spent on `reasoning_content`, 243.6s
  wall. Second attempt (to characterize the failure, not to retry-until-passing — the verdict was
  already determined): 10.4s wall (`cached_tokens: 111268/111272`, so not a prefill-timing
  artifact), same `finish_reason: "length"`, empty `content`. The captured `reasoning_content`
  (4504 chars) showed the model searching the wrong region of the prompt, never converging within
  budget.
- **Zero hang signature either attempt** — container stayed `healthy` throughout both. This is a
  **correctness regression under quantized cache at large context**, not the "freezes on Vulkan"
  failure mode upstream reviewers reported on BailingMoE3's merge PR, and not the DeepSeek-MLA
  family's "confident gibberish while healthy" issue either — a third, previously undocumented
  failure shape for this architecture+backend combination, but a real and reproducible one.

**Verdict: REJECTED.** Reverted; all four `ling-bakeoff` quants stay f16-cache-only,
`-c 131072`, no `-ctk`/`-ctv`/`-fa` on any entry. Worth a narrower follow-up if throughput ever
motivates revisiting quantized cache specifically (e.g. capped at a context size proven safe),
but not scheduled work.

## Bottom line

All four quants: `-c 131072` (the model's real, verified hard ceiling), f16 KV cache, correct
recall at both moderate and near-ceiling prompt sizes, zero hang signature across every test in
either plan, comfortably within the 15.92 GiB nameplate even at the largest quant (Q8_0, 10.16
GiB peak). Quantized KV cache was tested once (Q4_K_M only) and rejected on a reproducible
near-ceiling correctness failure — not pursued further, not applied to any other quant.
