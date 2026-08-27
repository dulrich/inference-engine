# Gemma4-12B QAT bake-off — results

First-ever run of the new serving-harness bake-off tooling (`llama/bakeoff/run_harness.py`, WP-1
of `plans/serving-harness-round-2.md`), against the new `gemma-4-12b-qat` testing alias (WP-2 of
the same plan). Raw JSON record: `llama/bakeoff/records/gemma-4-12b-qat-full/`. See also
`docs/ling-3-tiny-bakeoff-results.md` (Ling-3.0-tiny, round-2 retest),
`docs/qwen36-35b-a3b-bakeoff-results.md` (Qwen3.6-35B-A3B MoE), and
`docs/qwen38-27b-bakeoff-results.md` (Qwen3.8-27B dense).

## What this is

`unsloth/gemma-4-12B-it-qat-GGUF`'s only published quant (`UD-Q4_K_XL`, quantization-aware-trained
weights, 6,716,356,800 B / 6.26 GiB), added as a new no-consumer-yet testing alias
(`gemma-4-12b-qat`, `gemma-qat-bakeoff` group) — deliberately **not** a repoint of the live,
consumer-registered `gemma-4-12b` alias (the existing `gemma-4-12B-it-Q4_K_M`, post-training
quantized, untouched). Text-only for this round (no `--mmproj`); `-c 32768` and `--jinja` match
the live `gemma-4-12b` alias's own config exactly.

**Artifact lineage** (config.yaml `models.gemma-4-12B-it-qat-UD-Q4_K_XL`):
`hf_repo: unsloth/gemma-4-12B-it-qat-GGUF`, `hf_revision: 980b060c40a8539ac159e0501a3e0f66a6365af3`,
`sha256: 90fd44e29e0d7cffeb0fd00dc73cfdab9ed0b0e95306ecf7821ea634c940c370` (verified against HF's
own `x-linked-etag` header — matched exactly), `llama_cpp_build: b10534`.

## Methodology

- **VRAM**: `mem_info_vram_used` read directly, never assumed from weight size.
- **Cold-round mechanism**: container restart was judged too slow/heavy for 3 rounds; used the
  harness-sanctioned lighter proxy instead — a request into the `chat` group (`nemotron-4b`),
  which `exclusive: true` semantics evict `gemma-4-12b-qat` for. Verified live before the sweep
  that this genuinely flips `/v1/models`' `status.value` to `"unloaded"`, and the driver script
  (ad hoc, not committed — see below) polls for that confirmation before each cold round rather
  than assuming a fixed delay.
- **Needle probe**: only `"moderate"` (31000 tokens, ~94.6% of this model's real 32768 context
  ceiling) was run — a separate `"near_ceiling"` probe (85% of 32768 ≈ 27853 tokens) would be
  *smaller* than moderate for a model this size, not a genuine escalation. Worker-discretion call
  (plan Assumptions grants bounded probe-scope discretion); recorded in `config.json`'s `notes`.
- **Hang detection**: `journalctl -k -f` watched live throughout the entire sweep (download,
  first load, cold/warm/hot rounds, needle probe, quality corpus) — zero MES/wedged/reset/
  amdgpu-fault signature.
- **Quality corpus**: WP-1's 12 fixed prompts, `max_tokens: 768` (the harness's default), scored
  manually below (Decisions locked: no automated judge this round).

## VRAM

Baseline (nothing from `gemma-qat-bakeoff` loaded, `nemotron-4b` resident in `chat`):
4,080,046,080 B / 3.80 GiB. First `gemma-4-12b-qat` load (single-turn ping, weights 6.26 GiB):
9,395,146,752 B / 8.75 GiB. Post-sweep (after cold/warm/hot + needle + quality corpus, peak):
9,426,845,696 B / 8.78 GiB. Delta beyond weights alone ≈ 2.5 GiB (KV cache at `-c 32768` +
compute/graph buffer) — comfortably under both the 15.92 GiB nameplate and the realistic
free-VRAM budget on this desktop-shared host.

## Timing (cold/warm/hot, fixed ~5.3K-token performance fixture, 256-token output budget)

| Round | n | median TTFT (`first_generation_token_ms`) | median decode tok/s | cache assertion |
|---|---|---|---|---|
| cold | 3 | 6361.9 ms | 62.07 | n/a (no assertion defined for cold) |
| warm | 3 | 3471.1 ms | 61.95 | 3/3 passed (`cached_tokens/prompt_tokens < 0.05`) |
| hot | 3 | 254.3 ms | 63.04 | 3/3 passed (`cached_tokens/prompt_tokens > 0.90`) |

Real `prompt_tokens` per round: cold 5283, warm ~5320 (nonce-prefixed), hot 5283 — the fixed
~8K-token *target* tokenizes to ~5.3K real tokens for this model's tokenizer (character-per-token
estimate is architecture-dependent, per the harness's own documented caveat; the real count is
always read from the server's `usage` field).

**Open finding — `model_load_seconds` vs. the cold/warm TTFT gap.** Cold round 1's
`ResidencyPoller` (WP-1's candidate `model_load_seconds` mechanism) genuinely observed an
`"unloaded"→"loaded"` transition for the first time this plan — a real result, not `null` — and
measured it at **0.103 s**. But cold TTFT (6361.9 ms median) exceeds warm TTFT (3471.1 ms median,
same prompt size, model already resident) by ~2.9 s, far more than the poller's 0.1 s reading can
account for. Two candidate explanations, neither confirmed: (1) the sha256 lineage computation
run immediately before this sweep read the entire 6.26 GiB GGUF sequentially, likely leaving it
hot in the OS page cache — `llama-server`'s `mmap`-based load would then be near-instant
regardless of "real" cold-disk latency, which is consistent with the poller's own fast reading;
(2) llama-swap's subprocess spawn and/or Vulkan pipeline/shader-compilation phase may begin and
substantially complete *before* the child ever reports a poll-visible status transition, so the
poller's window doesn't cover the true start of the spawn. Recorded honestly rather than
resolved — `model_load_seconds: 0.103` is what was actually observed on cold round 1;
`first_request_e2e_ttft_ms` (6531.7 ms on that same round) is the number that should be quoted as
"cold latency" until this gap is understood, per the harness's own honest-timing-semantics
contract (never subtract one from the other to manufacture a cleaner number). Worth a follow-up
measurement (e.g. a genuine service restart between polls, avoiding the page-cache-warm
confound) if `model_load_seconds` needs to be trusted precisely in a later round — not attempted
further in this WP.

## Needle-in-haystack correctness

Moderate probe (31000-token target, 20330 real prompt tokens — see the character-per-token
caveat above): reference code `ZS3-CHARLIE-6408` embedded ~45% through the haystack, recalled
**correctly**, `finish_reason: "stop"`, 15.3 s wall.

## Quality corpus (12 prompts, manual scoring)

All 12 ran without transport/protocol error (`success: true` on every entry). Both sentinels
correct: `qa-2` → `391` (exact match); `longctx-1` → `GEN-88214` (exact match).

**Finding: 4 of 12 prompts (33%) hit the 768-token budget (`finish_reason: "length"`) before
finishing** — this model family's documented reasoning-verbosity pattern (already noted for the
live `gemma-4-12b` alias, which needs `max_tokens >= 128` for the same reason, and for
Ling-3-tiny-Q8_0, which needed 512), but more pronounced here: even 768 tokens wasn't always
enough. This is a measurement, not an abort trigger (Decisions locked, "Abort criteria
corrected") — no malformed output, no hang, every response terminated normally at its token
budget.

- `code-1`, `constraints-1`, `synthesis-1`: **empty visible `content`** — the entire 768-token
  budget was consumed by `reasoning_content` (2790/2746/4411 chars respectively) without ever
  reaching a `content` delta. A real UX-relevant finding for this quant/config: a caller using
  this model at 768 max_tokens can get a well-formed but entirely invisible response for a
  moderately complex prompt.
- `ambiguity-2`: substantial visible content, but cut off mid-sentence at the budget.
- The other 8 (including both structured-transformation and both factual-QA prompts) completed
  normally (`finish_reason: "stop"`) well within budget.

Per-prompt read (correctness/coherence, not the unpopulated 1–5 rubric fields — manual scoring
stays qualitative this round per Decisions locked):

| id | category | verdict |
|---|---|---|
| qa-1 | factual_technical_qa | Correct, concise (TCP vs UDP sequencing/reassembly). |
| qa-2 | factual_technical_qa | Correct (sentinel, exact match). |
| code-1 | code_debug_reasoning | Reasoning trace shows correct diagnosis in progress; truncated before any visible answer (see finding above). |
| code-2 | code_debug_reasoning | Correct: identifies the `range(n+1)` off-by-one bug and proposes the right fix. |
| constraints-1 | multi_constraint_instruction_following | Truncated before any visible answer (see finding above); reasoning trace shows it was tracking the constraints correctly. |
| transform-1 | structured_transformation | Correct, well-formed JSON. |
| transform-2 | structured_transformation | Correct, well-formed Markdown table. |
| ambiguity-1 | ambiguity_judgment | Reasonable: declines a bare yes/no, asks for the missing inputs needed to answer. |
| ambiguity-2 | ambiguity_judgment | Reasonable approach (asks clarifying questions) but response cut off mid-list. |
| correction-1 | correction_uptake | Correct population figure with the correction applied. |
| synthesis-1 | long_form_synthesis | Reasoning trace shows relevant synthesis in progress; truncated before any visible answer (see finding above). |
| longctx-1 | long_context_retrieval | Correct (sentinel, exact match). |

## Journal / hang check

`journalctl -k -f` watched continuously from before the container restart (config activation)
through the end of the quality corpus. Zero MES/wedged/reset/amdgpu-fault signature at any point.

## Summary

Clean first-ever onboarding of a new bake-off model family through the new harness: no hangs, no
VRAM surprises, correct needle recall, correct sentinels, and the harness's own honest-timing
mechanisms (cache assertions, `model_load_seconds`) all exercised against real data for the first
time. The main finding worth carrying forward is the reasoning-budget sensitivity (33% of the
quality corpus hit `length` at 768 tokens) — a genuine property of this model/quant, not a
harness or config defect.
