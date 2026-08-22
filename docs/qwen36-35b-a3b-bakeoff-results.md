# Qwen3.6-35B-A3B bake-off — results

WP-4 of `plans/serving-harness-round-2.md`: offload-placement search (configs A/B/C) plus a full
`llama/bakeoff/run_harness.py` (WP-1) run against the winning placement, landed as the new
`qwen36-a3b` testing alias (`qwen36-a3b-bakeoff` group). See also
`docs/gemma-4-12b-qat-bakeoff-results.md`, `docs/ling-3-tiny-bakeoff-results.md`, and
`docs/qwen38-27b-bakeoff-results.md` (Qwen3.8-27B dense).

## What this is

`unsloth/Qwen3.6-35B-A3B-GGUF`'s `UD-Q4_K_XL` quant (22,360,456,160 B / 20.83 GiB), added as a new
no-consumer-yet testing alias (`qwen36-a3b`). Architecture (`qwen35moe` per the GGUF header): a
hybrid of attention and SSM/recurrent layers (only 10 of 40 blocks carry conventional KV cache —
`full_attention_interval=4` — the rest are fixed-state recurrent layers, the same KV-lightness
pattern already seen in Ling-3.0-tiny's hybrid KDA/MLA architecture), 256 fine-grained routed
experts + 1 shared expert per MoE block, 8 experts active per token. Real context ceiling per the
GGUF header: 262,144 tokens — this WP's own scope target was the "~100K useful long-context"
point from `references/serving-harness-test-round-2.md` §3, not that full ceiling.

**Artifact lineage** (config.yaml `models.Qwen3.6-35B-A3B-UD-Q4_K_XL`):
`hf_repo: unsloth/Qwen3.6-35B-A3B-GGUF`, `hf_revision: 9280dd353ab587157920d5bd391ada414d84e552`,
`sha256: 707a55a8a4397ecde44de0c499d3e68c1ad1d240d1da65826b4949d1043f4450` (this value is also the
HF hub cache's own blob identifier for this file — an independent corroboration, not just a
self-computed number), `llama_cpp_build: b10534`. Repositioned from the HF cache into
`/srv/models/unsloth/Qwen3.6-35B-A3B-GGUF/` (WP-4 step 2), verified byte-identical (size
+ sha256) before the cache copy was removed.

## Methodology

- **Exact tensor-level GGUF math**, not a parameter-count estimate: a stdlib-only `struct` parser
  (same technique as Ling's context-ceiling research, no `gguf` pip package) read the GGUF
  tensor-info table and computed each tensor's real on-disk byte size from consecutive
  data-section offsets. Routed-expert weights: 18.320 GiB total across all 40 blocks (~491.8
  MiB/layer average, min 486.5/max 589.3 MiB). Non-expert weights (attention/ssm/embedding/
  output/shared-expert/router): 2.494 GiB, always GPU-resident at `-ngl 99` regardless of
  `-ncmoe`. This math sized the A/B/C candidates before any live load, rather than guessing.
- **Offload-placement search**: three scratch `llama-server` invocations on a throwaway port
  (`:8099`, reachable via the container's bridge IP since only the router's own port is
  host-published), never as permanent `config.yaml` groups, per Decisions locked. Live VRAM
  (`mem_info_vram_used`) and cgroup RAM (`memory.current`/`memory.max`/`memory.events`) read at
  each candidate's idle-loaded state and, for the two context-scaled configs, after a real
  near-ceiling needle probe.
- **Cold-round mechanism** (for the winning placement's full harness run, against the real router
  alias, not the scratch port): the same genuine-eviction proxy verified in WP-2/WP-3 (a request
  into the `chat` group's `nemotron-4b`, polled until `/v1/models` reports the target `unloaded`).
- **Hang detection**: `journalctl -k -f` watched live throughout the entire WP — placement search,
  config activation, and the full harness run. Zero MES/wedged/reset/amdgpu-fault signature at any
  point.
- **RAM sanity check** (plan step 1, cycle-2-corrected methodology): checked the least
  CPU-resident candidate that still fits VRAM (config A) rather than the most CPU-resident one —
  live-monitored via cgroup `memory.current`/`memory.max`/`memory.events` throughout, never just
  the upfront arithmetic. WP-0's 36 GiB cap made this pass comfortably at every candidate tested,
  including config C's full-CPU-offload worst case (28.4 GiB current / 36 GiB cap, 10+ GiB
  margin, zero OOM events).

## Offload-placement search (A/B/C)

| Config | Flags | GPU-resident experts | c=8192 VRAM (free) | c=100000 VRAM (free) | Decode tok/s (c=8192) | Near-ceiling needle | Verdict |
|---|---|---|---|---|---|---|---|
| A | `-ngl 99 -ncmoe 24` | 16 of 40 layers | 13.92 GiB (2.00 GiB) | not tested¹ | not tested | not tested | **REJECTED** — unsafe VRAM margin |
| B | `-ngl 99 -ncmoe 32` | 8 of 40 layers | 10.29 GiB (5.63 GiB) | 12.13–12.20 GiB (3.72–3.79 GiB) | ≈30.2 | ✅ correct | **WINNER** |
| C | `-ngl 99 -cmoe` | 0 of 40 layers | 6.59 GiB (9.33 GiB) | 8.43 GiB (7.49 GiB) | ≈27.2 | ❌ **FAILED**, reproducibly | REJECTED — correctness regression |

¹ Config A's c=32768 measurement (15,477,321,728 B / 14.42 GiB, 1.505 GiB free — exactly at the
plan's ~1.5 GiB VRAM-margin abort threshold) already showed the margin problem; the measured KV
growth rate on config A itself (160 MiB @ 8192 → 640 MiB @ 32768, an exact match to linear
scaling) predicts ≈0.47 GiB free at 100000 — a clear abort-territory number not worth a live
confirmation given the risk/cost of deliberately walking a live load into a known-tight VRAM edge
on this GPU.

### Config A — rejected (unsafe VRAM margin)

Maximal-GPU placement (`-ncmoe 24`, 16 of 40 layers' routed experts GPU-resident). Fits comfortably
at the 8K smoke-context point (2.00 GiB free) but the margin collapses to 1.505 GiB by the
moderate (32768) context point — right at this plan's VRAM-margin abort threshold before any
actual near-ceiling test. Not pursued to 100K given the linear KV-growth rate already measured on
this config predicts an unsafe ≈0.47 GiB margin there.

### Config B — winner

Attention/dense/recurrent hot path + 8 layers' routed experts on GPU, the other 32 layers'
routed experts in CPU RAM (`-ncmoe 32`). Healthy VRAM margin at every tested context point
(5.63 GiB free @ 8K, 3.72–3.79 GiB free @ 100K — buffers are pre-allocated at load time, not
growing further under real generation). RAM cgroup margin 10.28 GiB @ 100K under active load,
zero OOM events. Near-ceiling needle probe (100000 ceiling → 55,618 real prompt tokens, the
harness's tokenizer-dependent character-to-token gap already documented elsewhere in this plan)
recalled correctly, clean `stop` finish. Decode throughput ≈30.2 tok/s (200-token generation,
c=8192) — a real, usable rate for an expert-offloaded 35B-A3B model on this hardware, and the
config that satisfies every axis of the plan's own optimization target (quality-preserving quant
× useful context × acceptable TTFT × acceptable TG × stable VRAM) rather than maximizing GPU
residency for its own sake.

### Config C — rejected (correctness regression, not a fit problem)

All routed-expert weights CPU-resident (`-cmoe`), only attention/ssm/embedding/output/
shared-expert/router GPU-resident. By far the best VRAM/RAM headroom of the three (7.49 GiB free
@ 100K, more than double config B's), and only a modest throughput cost (≈27.2 vs B's ≈30.2
tok/s, ~10% slower) — on paper, a strong candidate. **Rejected on a reproducible correctness
failure**: the near-ceiling needle probe (100000 ceiling → ~55.5–55.6K real prompt tokens) failed
on two independent runs with two different embedded reference codes, both `finish_reason:
"length"` with empty visible `content`. The load-bearing detail: this is **not** the
already-documented reasoning-budget-truncation pattern seen elsewhere in this plan (a genuine,
on-topic reasoning trace cut off before reaching an answer) — the captured `reasoning_content` on
both failed probes was **degenerate regurgitation of haystack filler tokens** ("block interval
artifact archive cycle. driver archive engine artifact profile anomaly..."), never engaging with
the actual recall task. Zero hang signature, container stayed healthy throughout both probes —
the same "healthy but wrong" shape as the `ling-3-tiny-context-ceiling` plan's Part B
quantized-KV-cache rejection, but a distinct, previously undocumented failure mode: this model's
KV cache is f16 throughout on every config tested this WP; only expert-tensor placement varies,
and only the all-CPU placement exhibited this failure. Not investigated further at the
root-cause level (e.g. whether it's a numerical-precision difference between the Vulkan and CPU
compute paths for the offloaded expert math, or something else) — recorded as an honest,
reproducible finding per the harness's own philosophy of never papering over an unresolved
correctness question.

## Landed configuration

`-ngl 99 -ncmoe 32 -c 100000 --jinja` (config B), landed as `qwen36-a3b` in the
`qwen36-a3b-bakeoff` group. Activated via `docker compose restart inference-engine-llama`
(plain config-only restart — this is a bind-mounted `config.yaml` edit, not a `compose.fleet.yml`
resource-limit change, so it does not need `--force-recreate`), full `smoke-inference.sh` re-run
clean.

## Timing (cold/warm/hot, fixed ~8K-token-target performance fixture, 256-token output budget)

| Round | n | median TTFT (`first_generation_token_ms`) | median decode tok/s | cache assertion |
|---|---|---|---|---|
| cold | 3 | 14969.6 ms | 31.32 | n/a (no assertion defined for cold) |
| warm | 3 | 11947.8 ms | 31.50 | 3/3 passed (`cached_tokens/prompt_tokens < 0.05`) |
| hot | 3 | 104.1 ms | 31.76 | 3/3 passed (`cached_tokens/prompt_tokens > 0.90`) |

Cold TTFT (~15s) is far higher than any other model in this plan (Gemma4-12B-QAT: 6.4s; Ling
quants: 3.3–3.6s) — expected given the file size (20.83 GiB vs Gemma's 6.26 GiB / Ling's
4.8–8.4 GiB) and the genuine per-cold-round eviction+reload of the full weight set, 32 of 40
layers' experts landing in CPU RAM every cold round. Decode throughput (~31.3–31.8 tok/s across
all three run-states, stable) matches the scratch-port throughput measurement (≈30.2 tok/s)
closely, confirming the router adds negligible overhead. Warm TTFT (~12s) is still substantial —
consistent with this being a placement (not just weight-residency) reload: even a "warm" round
re-establishes the CPU-side expert placement from scratch each time under llama-swap's swap
semantics, unlike a purely-GPU-resident model where warm reload is closer to instant.

## Needle-in-haystack correctness (winning placement, real router alias)

**Moderate** (31000-token target): ✅ correct, clean `stop` finish.

**Near-ceiling** (100000 ceiling → 55,588 real prompt tokens): `finish_reason: "length"` at the
needle-probe's 256-token budget, empty visible `content` — but this is a **benign
reasoning-budget-truncation artifact, not a wrong answer**: the captured `reasoning_content`
shows the model correctly locating the code ("the exact reference code is FW9-DELTA-3123... I
will reply with only the code as requested... Response: FW9-DELTA-3123") and then burning the
rest of its budget on redundant self-verification ("Check format... Correct... Ready... Output
matches requirement... Done... [Final Check]... Matches perfectly... Proceeds...") without ever
transitioning to the `content` channel. This directly corroborates config B's earlier scratch-port
test (same context scale, clean `correct: True`, `stop` finish on a different embedded code) —
same placement, same genuine recall capability; this particular probe just didn't fit the tight
256-token budget. Explicitly **not** config C's degenerate-regurgitation failure (see the
config.yaml group comment's placement-search record for that distinction in detail).

## Quality corpus (12 prompts, manual scoring)

**Finding: this model exhibits the strongest reasoning-budget-truncation tendency of any model
tested in this plan.** At the harness's default 768-token budget: 9 of 12 prompts (75%) hit
`finish_reason: "length"`, and 6 of those 9 (50% of the full corpus) have **completely empty
visible `content`** — the entire budget spent on `reasoning_content`. For comparison: Gemma4-12B
QAT hit 33% (4/12, none 0%-visible beyond the 3 documented); Ling-3.0-tiny hit 8–17% depending on
quant. Both sentinels correct: `qa-2` → `391` (exact match); `longctx-1` → `GEN-88214` (exact
match) — both completed cleanly (`stop`), so the core factual-recall capability is intact; it is
specifically the more complex, multi-step prompts that trigger extended reasoning.

Manually inspected the reasoning traces of all three fully-empty complex prompts (`code-1`,
`constraints-1`, `synthesis-1`): all three are genuinely on-topic, coherent, carefully-structured
reasoning working correctly toward the right answer (e.g. `constraints-1`'s trace shows precise
word-by-word counting to satisfy the "under twelve words" constraint; `code-1`'s trace correctly
diagnoses the mutable-default-argument bug before running out of budget) — the same qualitative
shape as the reasoning-budget-truncation pattern already documented for Gemma/Ling, just far more
pronounced for this model. Not a coherence or correctness defect — a genuine property of this
model's reasoning verbosity relative to a 768-token budget.

| id | category | finish_reason | verdict |
|---|---|---|---|
| qa-1 | factual_technical_qa | length | Correct content as far as it goes (TCP sequencing/ack explanation), cut off mid-sentence. |
| qa-2 | factual_technical_qa | stop | Correct (sentinel, exact match). |
| code-1 | code_debug_reasoning | length | Reasoning trace correctly diagnoses the mutable-default-argument bug; truncated before any visible answer. |
| code-2 | code_debug_reasoning | length | Truncated before any visible answer (reasoning not inspected in detail — same budget pattern). |
| constraints-1 | multi_constraint_instruction_following | length | Truncated before any visible answer; reasoning trace shows correct constraint-tracking (precise word counts) in progress. |
| transform-1 | structured_transformation | length | Truncated before any visible answer. |
| transform-2 | structured_transformation | length | Partial table header only (`\| Item \| Price \|`) before truncation — visibly on the right track, incomplete. |
| ambiguity-1 | ambiguity_judgment | length | Substantial visible content (reasonable clarifying-question approach), cut off mid-list. |
| ambiguity-2 | ambiguity_judgment | length | Truncated before any visible answer. |
| correction-1 | correction_uptake | stop | Correct population figure with the correction applied, complete. |
| synthesis-1 | long_form_synthesis | length | Truncated before any visible answer; reasoning trace shows correct synthesis of all three viewpoints in progress. |
| longctx-1 | long_context_retrieval | stop | Correct (sentinel, exact match). |

## Journal / hang check

`journalctl -k -f` watched continuously from before the offload-placement search through the end
of the full harness run (config activation, cold/warm/hot, needle probes, quality corpus). Zero
MES/wedged/reset/amdgpu-fault signature at any point across the entire WP.

## Summary (canary quant, UD-Q4_K_XL)

Config B (`-ncmoe 32`) lands as `qwen36-a3b`: correct needle recall at both moderate and
near-ceiling context scales (the one near-ceiling "failure" on the real router being a budget
artifact with a verified-correct answer inside the reasoning trace, not a wrong answer), healthy
VRAM (3.72+ GiB free at 100K context) and RAM (10+ GiB free) margins, usable decode throughput
(~31 tok/s), and zero hang signature across the entire WP. Config A was rejected for an unsafe
VRAM margin at long context; config C was rejected for a reproducible correctness regression
(degenerate haystack-token regurgitation, not merely a truncated answer) despite superior
VRAM/RAM headroom — the clearest evidence in this plan yet that "maximal fit headroom" and
"correct at long context" are not the same axis, and that live near-ceiling correctness testing
(not just VRAM/RAM arithmetic) is load-bearing for placement decisions on a brand-new model
family. The main carry-forward finding is this model's pronounced reasoning-budget sensitivity
(75% of the quality corpus hit `length` at 768 tokens, worse than any other model this plan
tested) — a genuine property of this model, not a harness or placement defect.

## WP-4 step 5 — propagation to UD-Q5_K_XL / UD-Q6_K_XL

Same config-B placement policy propagated upward, `-ncmoe` recomputed per quant from each file's
own exact tensor-level GGUF math (fetched via an HTTP range request on the file header before the
full download completed, so the config could be written and the download run in parallel):
`qwen36-a3b-q5` (`-ncmoe 34`, UD-Q5_K_XL, 26,592,508,896 B / 24.77 GiB — sha256 verified against
HF's own recorded hash) and `qwen36-a3b-q6` (`-ncmoe 35`, UD-Q6_K_XL, 31,843,777,504 B / 29.66 GiB
— sha256 likewise verified). Both fresh downloads, both placed directly in
`/srv/models/unsloth/Qwen3.6-35B-A3B-GGUF/` (no HF-cache repositioning needed, unlike the
canary). Landed together with the canary's config.yaml edit, `docker compose restart` +
`smoke-inference.sh` clean, then the full WP-1 harness run against both real router aliases.

### Timing (cold/warm/hot, fixed ~8K-token-target performance fixture)

| Quant | Round | n | median TTFT | median decode tok/s | cache assertion |
|---|---|---|---|---|---|
| UD-Q5_K_XL | cold | 3 | 16423.4 ms | 27.72 | n/a |
| UD-Q5_K_XL | warm | 3 | 13242.7 ms | 27.80 | 3/3 passed |
| UD-Q5_K_XL | hot | 3 | 107.0 ms | 27.87 | 3/3 passed |
| UD-Q6_K_XL | cold | 3 | 18509.7 ms | 26.03 | n/a |
| UD-Q6_K_XL | warm | 3 | 15772.7 ms | 25.77 | 3/3 passed |
| UD-Q6_K_XL | hot | 3 | 116.3 ms | 25.87 | 3/3 passed |

Cold/warm TTFT scales with file size as expected (UD-Q4_K_XL ~15-12s → UD-Q5_K_XL ~16-13s →
UD-Q6_K_XL ~19-16s, tracking the larger weight sets' eviction/reload cost). Decode throughput
scales down modestly with quant size (~31 → ~28 → ~26 tok/s) — expected, more bytes moved per
token for the CPU-resident expert layers at higher precision, not a config regression.

### Needle-in-haystack correctness — a cross-quant pattern, investigated and resolved

Raw results: UD-Q5_K_XL moderate `correct: false`, near-ceiling `correct: false` (55,739 real
prompt tokens); UD-Q6_K_XL moderate `correct: true`, near-ceiling `correct: false` (55,594 real
prompt tokens). Combined with the canary's own near-ceiling "failure", every near-ceiling probe
across all three quants and one of two moderate probes came back `correct: false` — investigated
in full rather than accepted at face value, since this is exactly the shape config C's real
correctness regression took.

**Verdict: all four `false` results are the benign reasoning-budget-truncation pattern, not a
repeat of config C's failure.** Read every reasoning trace directly:

- UD-Q5_K_XL near-ceiling and UD-Q6_K_XL near-ceiling: both traces correctly locate and state the
  reference code ("the exact reference code is FM9-NOVA-3174... I need to extract the code",
  "Found it: `Reference code for this section: JK7-LUMEN-8676`... The code is `JK7-LUMEN-8676`")
  before running out of the needle-probe's 256-token budget mid-formatting — identical shape to
  the canary's own near-ceiling result.
- UD-Q5_K_XL moderate: the reasoning trace explicitly finds the code, states it, and writes
  "Done." ("The exact code is 'HZ7-CHARLIE-0987'... So the answer should be exactly
  'HZ7-CHARLIE-0987'. Done.") — then continues into a self-verification step ("Let's double
  check. Text snippet: ...") that quotes a chunk of haystack filler text and runs out of budget
  mid-quote. Read only the trailing 600 characters in isolation, this looks identical to config
  C's degenerate haystack regurgitation — reading the **full** trace shows it is a real,
  goal-directed verification step that happens to quote filler text, occurring *after* the model
  had already stated the correct answer. This is the load-bearing distinction from config C's
  actual failures, where the reasoning trace never engaged with the task at all.
- UD-Q6_K_XL moderate (the one that returned `correct: true`) shows the same shape without
  hitting the budget: finds the code, states it, verifies once, and stops.

Both sentinels correct in the quality corpus for both quants (`qa-2` → `391`, `longctx-1` →
`GEN-88214`), consistent with the canary. Quality-corpus `length` rate: UD-Q5_K_XL 9/12 (matching
the canary's 75%), UD-Q6_K_XL 7/12 (58%, somewhat better) — the reasoning-budget-sensitivity
finding holds across all three quants, not a canary-specific artifact.

### Summary (Q5/Q6 propagation)

The propagated placement policy (config B, `-ncmoe` scaled per quant's measured routed-expert
tensor size) works correctly on both larger quants: real correct recall at both context scales
once the reasoning-budget-truncation artifact is accounted for, both sentinels correct, zero hang
signature, cache assertions clean, decode throughput degrading gracefully (not cliff-dropping)
with quant size. No placement re-tuning was needed beyond the `-ncmoe` value itself.
