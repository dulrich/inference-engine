# Qwen3.8-27B dense bake-off — results

WP-5 of `plans/serving-harness-round-2.md`: 3-bit fit/quality frontier for a dense 27B model on
this 16 GiB card, run under the standard benchmark profile as the default operating state. See
also `docs/gemma-4-12b-qat-bakeoff-results.md`, `docs/ling-3-tiny-bakeoff-results.md`,
`docs/qwen36-35b-a3b-bakeoff-results.md`.

## What this is

`unsloth/Qwen3.8-27B-GGUF`, three quants: `UD-IQ3_XXS` (10,934,860,704 B / 10.18 GiB),
`UD-IQ3_S` (12,040,883,104 B / 11.21 GiB, the canary), `UD-Q3_K_XL` (13,146,393,504 B /
12.24 GiB). Architecture (`qwen35` per the GGUF header): a **dense sibling of Qwen3.6-35B-A3B's
`qwen35moe`** — same hybrid attention+SSM/recurrent layer mix (`full_attention_interval=4`, 65
blocks → 16 real-attention layers, the rest fixed-state recurrent), no MoE, no `-ncmoe`, full
`-ngl 99`. Real context ceiling per the GGUF header: 262,144 tokens — not reachable on this card
(see below).

**Artifact lineage**: `hf_repo: unsloth/Qwen3.8-27B-GGUF`, `hf_revision:
4ca720788d1e01f1bff70c033e0d0028fd02e502`, all three quants sha256-verified byte-identical
against HF's own recorded hash before use. `llama_cpp_build: b10534`.

## Methodology

- **VRAM fit-finding via scratch server**, same throwaway-port technique as WP-4's placement
  search — not a placement search here (dense, nothing to search), but a genuine "what context
  can this card actually hold" investigation, since the plan's own `-c 100000` target proved
  unsafe.
- **A genuine session confound found and fixed**: an early RAM-cgroup reading looked marginal
  (0.53 GiB free of the 36 GiB cap, `memory.events` "max" incrementing) — traced to accumulated
  page cache from this session's earlier WP-4 model loads (~80 GiB of GGUF reads), not this
  model's own footprint. `POST /api/models/unload` (llama-swap's genuine unload-all-resident
  endpoint, discovered this WP — more reliable than the exclusive-group-eviction trick for a true
  empty baseline) cleared VRAM but not the stale page cache; a `docker compose up -d
  --force-recreate` (the same mechanism WP-0 used for `HostConfig` changes) was needed for a
  genuinely fresh cgroup. After that, RAM was never actually a binding constraint for this model
  (26+ GiB margin throughout, zero `memory.max` events) — **VRAM was the real, reproducible
  constraint**, unaffected by the cgroup confound (VRAM baseline measurements were identical
  before and after the recreate, confirming the two resources are genuinely independent here).
- **KV cache growth confirmed exactly linear** (0.0625 MiB/token) across three context points on
  the canary (512 MiB @ 8192, 4096 MiB @ 65536, 6256 MiB @ 100000) — used to compute per-quant
  safe context ceilings from live measurements rather than guessing.
- **Hang detection**: `journalctl -k -f` watched live throughout the entire WP (fit-finding,
  config activation, full harness runs for all three quants). Zero MES/wedged/reset/amdgpu-fault
  signature — including one benign kernel `hrtimer` latency note under load, not a hang
  signature.
- **Cold-round mechanism** (full harness runs, real router aliases): the same genuine-eviction
  proxy verified in WP-2/3/4.

## VRAM fit-finding — why the landed `-c` values are well below the plan's 100K target

Card nameplate: 15.92 GiB. The plan's own `-c 100000` target, tested live on the canary
(UD-IQ3_S), measured only **0.10 GiB free** — investigated rather than accepted, since the KV
growth rate was already confirmed linear and the shortfall was real. Progressively more
conservative candidates were live-tested:

| Context | Margin (canary UD-IQ3_S) | Verdict |
|---|---|---|
| 100000 | 0.10 GiB | REJECTED — at the plan's own abort threshold |
| 65536 | 0.32 GiB | REJECTED — still under the ~1.5–2 GiB safety-margin convention |
| 49152 | 1.34 GiB | REJECTED — still under the ~1.5–2 GiB convention |
| **40960** | **1.85 GiB (idle) / 1.83 GiB (peak, after a real needle-probe generation)** | **LANDED** |

`UD-IQ3_XXS` (the smallest quant) at the same `-c 40960`: 2.76 GiB free — comfortable margin,
correct recall. `UD-Q3_K_XL` (the largest quant) needed to step down to **`-c 24576`** for
comparable headroom (1.83 GiB free) — its ~1 GiB larger weight footprint directly consumes the
margin `UD-IQ3_S` had at 40960. All three per-quant ceilings were live-verified with a real
moderate needle-probe generation before landing, not just an idle-load VRAM reading.

## Landed configuration

`qwen38-27b-bakeoff` group: `qwen38-27b-iq3xxs` and `qwen38-27b-iq3s` at `-c 40960`,
`qwen38-27b-q3kxl` at `-c 24576`, all `-ngl 99 --jinja`, no offload flags (dense). Activated via
`docker compose restart` (config-only), full `smoke-inference.sh` clean.

## Timing (cold/warm/hot, fixed ~8K-token-target performance fixture)

| Quant | Round | n | median TTFT | median decode tok/s | cache assertion |
|---|---|---|---|---|---|
| UD-IQ3_XXS | cold | 3 | 9936.8 ms | 37.23 | n/a |
| UD-IQ3_XXS | warm | 3 | 7040.5 ms | 37.11 | 3/3 passed |
| UD-IQ3_XXS | hot | 3 | 138.8 ms | 36.95 | 3/3 passed |
| UD-IQ3_S | cold | 3 | 11176.0 ms | 34.70 | n/a |
| UD-IQ3_S | warm | 3 | 7198.6 ms | 34.64 | 3/3 passed |
| UD-IQ3_S | hot | 3 | 140.3 ms | 34.55 | 3/3 passed |
| UD-Q3_K_XL | cold | 3 | 11174.6 ms | 32.34 | n/a |
| UD-Q3_K_XL | warm | 3 | 7214.7 ms | 32.28 | 3/3 passed |
| UD-Q3_K_XL | hot | 3 | 143.8 ms | 32.18 | 3/3 passed |

Decode throughput scales down smoothly with quant precision (37.2 → 34.6 → 32.2 tok/s) — no
cliff, consistent with this being a fully GPU-resident dense model (no CPU-offload compute
bottleneck the way the A3B models had). Cold/warm TTFT track file size as expected. All 18 cache
assertions (3 quants × warm+hot × 3 rounds) passed at the harness's pinned thresholds.

## Needle-in-haystack correctness

**All 6 probes (3 quants × moderate + near-ceiling) recalled correctly** — the cleanest result of
any model in this plan, no budget-truncation artifacts on any needle probe:

| Quant | Moderate | Near-ceiling (real `prompt_tokens`) |
|---|---|---|
| UD-IQ3_XXS | ✅ correct | ✅ correct (22,841, ctx=40960) |
| UD-IQ3_S | ✅ correct | ✅ correct (22,893, ctx=40960) |
| UD-Q3_K_XL | ✅ correct | ✅ correct (13,772, ctx=24576) |

## Quality corpus (12 prompts × 3 quants, manual scoring)

Both sentinels correct on every quant: `qa-2` → `391` (exact match); `longctx-1` → `GEN-88214`
(exact match). Truncation rate is by far the lowest of any model in this plan: `code-1` and
`synthesis-1` hit `length` with empty visible content on **all three quants** (a genuine,
prompt-specific reasoning-budget-sensitivity finding, consistent across quants — 2/12, 17%,
matching Ling-3.0-tiny's rate, far better than the A3B family's 58–75%); `UD-IQ3_S` additionally
truncated `ambiguity-2` but with substantial visible content already written (not empty). The
other 9–10 of 12 prompts per quant completed normally (`finish_reason: "stop"`) with correct or
reasonable content on manual inspection — factual QA, code-debug (the non-truncated `code-2`),
structured transformation, ambiguity judgment, correction uptake, and long-context retrieval all
read correctly across all three quants, consistent with each other (e.g. `code-2`'s off-by-one
diagnosis is materially identical across all three, just phrased differently).

## Journal / hang check

`journalctl -k -f` watched continuously from before the VRAM fit-finding through the end of the
full harness run for all three quants. Zero MES/wedged/reset/amdgpu-fault signature at any point.

## Summary

Dense 27B is genuinely viable on this 16 GiB card at 3-bit quantization, but **not** at the
plan's original `-c 100000` aspiration — VRAM is the hard, reproducible binding constraint (not
RAM, despite an early false alarm this WP traced to stale session-wide page cache and fixed with
a container recreate), and the real safe ceiling is `-c 40960` for the two smaller quants,
stepping down to `-c 24576` for the largest. Within those live-verified ceilings, results are the
cleanest of any model in this plan: all 6 needle probes correct, both sentinels correct on all
three quants, decode throughput degrading smoothly (not cliff-dropping) with quant precision, and
the lowest quality-corpus truncation rate of any model tested (17% vs. the A3B family's
58–75%) — this dense architecture is markedly less prone to the reasoning-budget-truncation
pattern documented elsewhere in this plan, though it still exhibits it on the same two prompts
across all three quants.

---

# KV-cache quantization follow-on (UD-IQ3_S only) — `plans/qwen38-27b-kv-cache-quantization.md`

WP-2/WP-3 of a follow-on plan (2026-08-21/22): can KV-cache quantization raise `UD-IQ3_S`'s
practical context ceiling from the `-c 40960` f16-cache landing above toward a genuine ~100K
*actual*-token context, while holding decode throughput and correctness? Scope is `UD-IQ3_S`
only — `UD-IQ3_XXS`/`UD-Q3_K_XL` and the Qwen3.6-35B-A3B family were not re-tested and remain on
their f16-cache configs above.

## Tested candidates and exact flags

All candidates tested via a scratch `llama-server` on a throwaway port (own stdout/stderr log,
never the router's `/logs`), `journalctl -k -f` watched continuously, zero hang signature
throughout, `gpu-hang-watchdog.service` re-checked before/after every load.

| Candidate | Flags | KV cache (measured, scratch-log verbatim) | Result |
|---|---|---|---|
| A (baseline) | f16/f16, `-c 40960` (already landed above) | — | Reused existing WP-5 numbers, not re-run |
| B — `q8_0/q8_0` | `-ctk q8_0 -ctv q8_0 -fa on` | `K (q8_0): 680.00 MiB, V (q8_0): 680.00 MiB` @ c=40960 (1360.00 MiB total) | Correct + `flash_attn=enabled` (no fallback), but **fails the fit bar**: safe real-token ceiling ≈62–65K under the 1.5–2 GiB margin convention (c=90000 measured only 1.38 GiB free) — short of the ~100K target |
| D — `q4_0` K / f16 V | `-ctk q4_0 -ctv f16 -fa on` | `K (q4_0): 360.00 MiB, V (f16): 1280.00 MiB` @ c=40960 (1640.00 MiB total) | **Rejected analytically, no live probe run**: 42.0 KB/cell vs. B's 34.8 KB/cell — D costs *more* VRAM per context cell than B, since V stays f16 and dominates cache size; cannot fix a fit problem B already had |
| **"E-alt" — `q4_0/q4_0`** (see deviation note below) | `-ctk q4_0 -ctv q4_0 -fa on -c 120000` | `K (q4_0): 1055.25 MiB, V (q4_0): 1055.25 MiB` @ c=120064 (2110.50 MiB total), confirmed identical across all 4 loads (initial + 3 cold-round restarts) | **Qualifies — landed** |

**Deviation, documented explicitly**: the plan's literal candidate "E" was "a mixed q4/q8
combination." A literal mixed (asymmetric-type) pair was expected, per the plan's own audit
disposition, to hit `plans/ling-3-tiny-context-ceiling.md`'s documented non-fused-attention-
fallback penalty for a mismatched quantized K/V pair (its own tested case: `q8_0` K / `q4_0` V) —
a low-expected-value test. D's own data showed the actual fix needed is quantizing V (the dominant
cost), so **symmetric `q4_0/q4_0`** was tested instead — matched types, like B's own `q8_0/q8_0`,
confirmed live (not assumed) to keep `flash_attn` fused with zero fallback on every load. Full
rationale: `runs/dispatch/wp-2-report.md`.

## Proof the landed KV type is actually active

Scratch-server log evidence (candidate testing) is direct and verbatim — see table above. For the
**router-managed alias post-landing**, `fetch_router_logs`/`docker logs` were tried and found to
**never surface a router-managed child's own stdout at all** (confirmed with and without `-v` —
`-v` does make `llama-server` itself emit the `type_k`/`type_v`/`flash_attn` lines, but llama-swap
never forwards them into `/logs` or `docker logs` regardless), so live verification instead used
**VRAM arithmetic**: a fresh cold load of the landed alias measured **13.720 GiB** total
(weights 11.214 GiB + 2.506 GiB non-weight overhead). The scratch-verified `q4_0/q4_0` cache at
this exact context (2110.50 MiB) accounts for all but ~456 MiB of that overhead (a plausible
compute/graph buffer) — and f16/f16 KV at `-c 120000` would need ~7.34 GiB (this plan's own
measured linear 0.0625 MiB/token rate), which would put total VRAM at ~18.99 GiB against a
15.92 GiB card nameplate — i.e. it would not have loaded at all. **The clean load itself is
quantitative proof the intended quantized cache is active**, not an assumption from the request
flags.

## Long-context correctness (tokenizer-calibrated, not the flat character estimate)

The bake-off harness's flat `4.5 chars/token` estimate was independently confirmed (this plan's
own WP-1) to be off by a live-measured factor for this tokenizer (empirical ratio **0.678**) — all
long-context claims below use the new live-calibration mechanism, not the nominal target.

| Probe | Real `prompt_tokens` | Target | Tolerance met | Result |
|---|---|---|---|---|
| Moderate (uncalibrated) | 20,350 | ~31K nominal | n/a | ✅ correct |
| Calibrated ~100K | **96,578** | 100,000 (±10%) | ✅ `true` | ✅ correct (`JF3-NOVA-5858` recalled exactly) |

## VRAM

Loaded (fresh, `-c 120000`, `q4_0/q4_0`): **13.720–13.779 GiB** → margin **2.14–2.21 GiB** against
the 15.92 GiB card nameplate — meets and slightly exceeds the ~1.5–2 GiB safety-margin convention.

## Timing (cold/warm/hot, genuine per-round scratch-process restarts for cold)

| Round | n | median TTFT | median decode tok/s | cache assertion |
|---|---|---|---|---|
| cold | 3 | 6912.0 ms | 34.94 | n/a |
| warm | 3 | 7108.0 ms | 34.80 | 3/3 passed |
| hot | 3 | 142.3 ms | **34.37** | 3/3 passed |

Hot-median decode (34.37 tok/s) is **0.5% below** the f16 baseline's 34.55 tok/s hot-median —
essentially throughput parity, comfortably inside the plan's 15% "close" band. Full record:
`llama/bakeoff/records/qwen38-27b-iq3s-kv-e-alt-q4q4-c120000/`.

## Quality comparison vs. the f16 baseline

8/12 quality-corpus prompts tested (all 8 categories covered), each compared against the f16
baseline's completions for the identical prompt: both sentinels exact-match (`qa-2`→`391`,
`longctx-1`→`GEN-88214`); all 6 open-ended prompts materially equivalent in content and quality
(e.g. `code-2`: identical off-by-one diagnosis and fix; `transform-1`: identical JSON fields,
differing only in pretty- vs. compact-printing). One item (`synthesis-1`) truncated at the
harness's default `max_tokens=768` — but the **f16 baseline shows the identical truncation on the
identical prompt at the identical budget**, proving this is a pre-existing harness-default-budget
artifact affecting both configurations equally, not a KV-quantization regression; re-run at
`max_tokens=3000` produced a fully coherent, correct, on-task ~180-word synthesis
(`finish_reason: stop`). **Zero material quality regression found.**

## Recommendation and what landed

**Landed**: `qwen38-27b-iq3s` now runs `-c 120000 -ctk q4_0 -ctv q4_0 -fa on` (was `-c 40960`,
f16/f16). Nearly **3x the usable context** (96,578 real tokens recalled correctly vs. the prior
f16 config's own 22,893-real-token near-ceiling probe) at **essentially unchanged decode
throughput** (34.37 vs. 34.55 tok/s hot-median, 0.5% difference) and **no correctness regression**
on any tested axis. `UD-IQ3_XXS`/`UD-Q3_K_XL` and the Qwen3.6-35B-A3B family were out of this
plan's scope and remain unchanged (f16 cache, their original `-c` values above).
