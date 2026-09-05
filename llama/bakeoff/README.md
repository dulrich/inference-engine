# llama/bakeoff — serving-harness bake-off tooling

Reusable measurement tooling, originally built in `plans/serving-harness-round-2.md` WP-1 and
called by every later per-model WP against a different already-configured router alias, then
extended into a declarative sweep framework by `plans/quant-sweep-harness-and-instrument-v2.md`
WP-1, and into a valid long-context correctness instrument by that same plan's WP-3
(`longcontext.py` — see "Long-context correctness instrument v2" below). There is no
alias-specific code path anywhere in `run_harness.py`/`longcontext.py` — the harness works
against any alias the router already knows how to spawn.

Zero new runtime dependencies: Python 3 stdlib only (`urllib`, `json`, `time`, `statistics`,
`hashlib`, `threading`, `argparse`, `random`, `uuid`, `struct`, `subprocess`, `re`, `shlex`,
`signal`). No `requests`, no `gguf`, no `numpy`, no venv, no `requirements.txt` — a
from-scratch worker can run this with nothing beyond what's already on the host image.

## Files

29 production modules. Several CLI entry points own the run loops and argparse surfaces;
everything else is a module they compose. The `*_`-prefixed families are the WP-R1..R4 splits of
`plans/repository-tooling-structural-remediation.md` — each extracted module names its origin and
its review finding in its own docstring, so `head -3 <module>.py` tells you why it exists.

**Entry points** (`python3 <file>` — all of these are runnable):

- `batch.py` — queue-driven unattended batch runner: turns a self-contained queue file into a
  durable, resumable run. Owns only the run loop and the CLI.
- `sweep.py` — declarative sweep framework and the `--dry-run` cell expander.
- `longcontext.py` — long-context correctness instrument v2 (haystack + battery + position sweep).
- `run_harness.py` — serving-harness bake-off: timing sweep, needle probe, quality corpus.
- `verify_run.py` — post-queue mechanical verification of a finished run directory.
- `control_dispatcher.py` — host control-plane dispatcher CLI and the `run-window.sh` adapters.

**Queue-cell core** (WP-R1, extracted from `batch.py`):

- `cell_model.py` — the typed immutable cell values. `CellIdentity` carries exactly the four
  fields `compute_cell_id` hashes and physically cannot carry more.
- `cell_spec.py` — the plain-dict sweep-cell normal form: defaults, validation, deterministic
  identity, and the exact `llama-server` argv a cell renders to.
- `queue_file.py` — queue-file parse/validate/resolve, and the HH:MM deadline.
- `queue_state.py` — the durable `.state.json` machine: init/load/atomic-save, the `interrupted`
  rule, both re-arm forms, skip propagation.
- `cell_outcome.py` — the outcome-precedence table, as a pure function.
- `cell_record.py` — record persistence and the pre-enqueue size projection.
- `cell_runner.py` — the live runners: startup gate → optional timing → optional battery.
- `hang_rail.py` — the unattended safety rail: kernel hang-signature detection.

**Measurement harness** (WP-R2, extracted from `run_harness.py`):

- `harness_transport.py` — streaming HTTP transport, residency polling, timing rounds, live
  tokenizer probes.
- `harness_record.py` — schema-v2 experiment-record serialization.
- `harness_fixture.py` — shared paths, constants, the fixed cold/warm/hot performance fixture.
- `harness_cli.py` — the argparse surface and command composition.

**Long-context instrument** (WP-R2, extracted from `longcontext.py`):

- `longcontext_corpus.py` — corpus loading, deterministic haystack assembly, needle injection.
- `longcontext_tasks.py` — the 3-task battery (literal recall, ordered readback, anchored QA).
- `longcontext_classifier.py` — per-trial verdicts, the degeneracy classifier, Wilson-interval
  statistics, and the deterministic per-trial seed schedule.
- `longcontext_cell.py` — the prompt-fit shrink loop, the n-trial cell driver, the position sweep.

**Fit math and server lifecycle** (WP-R3, extracted from `sweep.py`):

- `gguf_fit.py` — pure GGUF geometry, KV-cache byte math, VRAM budgets, the fit pre-filter.
- `scratch_lifecycle.py` — scratch-server spawn/bridge-IP discovery/health-poll/startup
  verification/teardown. Health is judged **by response body, never status code**.

**Control plane** (WP-R4, extracted from `control_dispatcher.py`):

- `control_protocol.py` — the on-disk protocol: schema names, id/stem grammars, bounds, timestamp
  spelling, and the single validator that decides whether an untrusted request is admissible.
  `control-request-v1` is enforced here, not merely annotated.
- `control_store.py` — durable file primitives (atomic write, claim, disposition).
- `control_phases.py` — the dispatcher's six phases and its crash recovery.
- `control_process.py` — process/boot identity reads.

**Other:**

- `sampling_table.py` — the per-model-family recommended "production" sampling table.
- `gates.sh` — the executable form of this directory's gate list (see `AGENTS.md`).
- `pyproject.toml` / `uv.lock` — **dev-only** Ruff + Pyright configuration. The runtime stays
  Python-stdlib-only; nothing here is a runtime dependency.

**Data and tests:**

- `tests/` — stdlib `unittest` suite (574 tests) covering all of the above. Run it from the repo
  root; the summary goes to **stderr**, because the suite prints JSON verification blocks to
  stdout:
  `python3 -m unittest discover -s llama/bakeoff/tests -p 'test_*.py' 2>&1 >/dev/null | tail -8`
- `corpus/` — the coherent public-domain haystack corpus (Project Gutenberg) `longcontext.py`
  assembles trials from, plus `corpus/manifest.json`'s provenance record.
- `examples/sweep-example.json` — a 3-cell fixture for `sweep.py --dry-run` (synthetic geometry,
  no real model file required).
- `queues/` — committed queue files and their durable `.state.json` run state. **State files are always tracked, at every `queue_state`** — commit a `.state.json` whenever it changes (a `paused-resumable` checkpoint between windows is committed like any other data), so a queue never leaves an untracked file in the tree. User decision 2026-09-01, superseding the earlier terminal-only convention (`fb5465e`) under which live state stayed untracked until the queue completed.
- `quality_corpus.jsonl` — 12 fixed prompts across the 8 categories in
  `references/serving-harness-test-round-2.md` §5, including 2 deterministic sentinels (`qa-2`,
  `longctx-1`) that do not dominate the set. Reused identically across every quant of a given
  model — do not parameterize it per-model.
- `records/<experiment-id>/` — raw experiment records, tracked and committed (see the
  "Honest-timing-semantics contract" below and the remediation plan's Decisions-locked section
  for why this reverses the more common "runs/ is scratch" convention).

## The honest-timing-semantics contract (read before changing timing code)

A single client request cannot generally separate "time spent loading the model" from "time
spent on first-token latency" unless the router exposes an independently observable load-ready
transition. This harness never assumes that separation is free:

- **`model_load_seconds`** is populated **only** if `ResidencyPoller` (a background thread
  polling `GET {base_url}/models` for the alias's `status.value`) actually observed a transition
  away from `"loaded"` before observing it flip back to `"loaded"`. If the alias was already
  resident when the poller started, or the transition happened between two polls, this field is
  `null`. **Do not "fix" a null value by subtracting an assumed constant from another timing
  field** — that was rev 1's rejected assumption (plan Decisions locked / Assumptions). A
  reviewer should revert any change that does this. **The name is reserved**: the separate
  spawn → startup-verification measurement added 2026-08-25 is called `scratch_startup_seconds`
  and lives on `config.json`, precisely so it can never be mistaken for this field (see the
  dated additive-field note in "Schema v2" below).
- **`first_request_e2e_ttft_ms`** is always populated (request issued → first generated token of
  any kind) and, for a genuinely cold round, includes model load time inside it. This is the
  number every WP actually reports whenever `model_load_seconds` stays `null`.
- **`warm_ttft_ms`** / **`hot_ttft_ms`** are the same measurement as
  `first_generation_token_ms`, labeled per round type; they are asserted against *real* cache
  evidence, not merely "should have missed/hit the cache per prompt design": warm rounds assert
  `cached_tokens / prompt_tokens < 0.05`, hot rounds assert `cached_tokens / prompt_tokens >
  0.90`. A round that fails its own assertion is flagged (`cache_assertion_passed: false`), never
  silently folded into an aggregate.
- **Reasoning-model-aware timestamps**: `first_generation_token_ms` fires on the first
  non-empty delta of *any* kind (`reasoning_content` or `content`); `first_visible_content_ms`
  fires only on the first non-empty `content` delta. A reasoning model can spend its entire
  `max_tokens` budget on `reasoning_content` and never reach `content` — `first_visible_content_ms`
  stays `null` in that case, which is correct, not a bug (observed live against `nemotron-4b`
  during this WP's own dry run).
- Server-reported `timings.prompt_n/prompt_ms/prompt_per_second` and
  `timings.predicted_n/predicted_ms/predicted_per_second` (llama.cpp's own prompt-eval and
  decode counters) are retained verbatim alongside the client-side timestamps under
  `server_prompt_eval_*` / `server_eval_*` — never substituted for the client-side numbers. They
  answer a different question (backend kernel throughput vs. user-visible latency).

## Wire-format notes (verified live against this repo's router, `b10534`)

Request `stream_options: {"include_usage": true}` on every streaming request — without it,
llama.cpp's server does not emit the final `usage`/`timings` chunk at all. That final chunk has
an **empty** `choices` array; `usage.prompt_tokens_details.cached_tokens` is the field this
harness treats as the primary cache-hit signal (`timings.cache_n` is used as a fallback if the
former is absent — the router does not currently expose a separate `cache_hit_ratio` field, so
the harness always computes the ratio itself from `cached_tokens / prompt_tokens`).

## Usage

### Cold/warm/hot timing sweep

```sh
python3 llama/bakeoff/run_harness.py sweep --alias nemotron-4b
```

Runs the fixed performance fixture (`FIXTURE_PROMPT_TEXT`, ~8K input tokens target,
`FIXTURE_OUTPUT_TOKENS` = 256 output tokens, both named module constants — never re-derived per
call) through 3 cold, 3 warm, then 3 hot rounds, and prints the per-round samples plus an
n/median/mean/min/max/stdev summary for `first_generation_token_ms` and
`observed_decode_tok_s` per round type, as JSON to stdout.

**Cold rounds and residency**: this harness does not itself force a model-not-resident state
between cold rounds — pass a `cold_precondition_hook` (Python API only; the CLI runs the default
of "no hook") that performs whichever mechanism the calling WP has chosen (container restart, or
an `exclusive: true` group eviction via a request into a different group — plan Assumptions:
either satisfies the same observable contract). Without a hook, only the very first cold round of
a fresh alias is genuinely cold.

**Hot-phase cache priming**: an explicit, unrecorded priming request (`round_index: 0`, tagged
with a `note` field) fires immediately before the 3 measured hot rounds, using the identical
fixture text. This makes hot-phase correctness independent of what ran immediately before it
(specifically: independent of whether warm ran before or after) — no need to rely on execution
order to guarantee the cache is primed correctly.

**Warm-round novelty**: rather than regenerating ~8K tokens of fresh filler per warm round (slow,
and not actually necessary), each warm round prepends a short unique nonce line to the same fixed
fixture body. Changing the prefix from token 0 guarantees a cache miss regardless of what's
currently cached, while keeping the total token count essentially unchanged.

### Needle-in-haystack correctness probe

```sh
python3 llama/bakeoff/run_harness.py needle --alias nemotron-4b --size moderate
python3 llama/bakeoff/run_harness.py needle --alias qwen36-a3b-config-b --size near_ceiling --context-ceiling 131072
```

Embeds a unique reference code (shape matches this repo's established pattern, e.g.
`XK7-DELTA-9042` — see `docs/ling-3-tiny-bakeoff-results.md`) ~45% through a constructed filler
haystack, asks the model to recall it. `moderate` targets ~31K tokens (plan's ~29-33K band);
`near_ceiling` requires `--context-ceiling` (the target model's real, verified context length —
never a fixed number) and targets 85% of it, leaving headroom for the question and a
reasoning-model's own budget. A wrong-but-well-formed answer is recorded (`correct: false`), not
raised as an exception — the CLI's exit code reflects it (0 = correct, 1 = incorrect) but nothing
in the library layer aborts. The haystack text itself is not persisted verbatim in the record
(it would dominate the 25 MiB experiment size budget at near-ceiling scale) — the generation
parameters (`seed_used`, `target_tokens`, `position_fraction`) are, and are sufficient to
regenerate the identical haystack deterministically.

### Quality corpus

```sh
python3 llama/bakeoff/run_harness.py quality --alias nemotron-4b
```

Runs all 12 fixed prompts from `quality_corpus.jsonl` once each, prints raw completions plus
per-answer records with the 1–5 rubric fields (`correctness`, `instruction_following`,
`completeness`, `reasoning`) and `critical_error`/`pairwise_preference` as `null` placeholders —
scoring stays manual/raw-capture this round (Decisions locked); no automated judge is built here.
`sentinel_correct` is populated automatically for the two sentinel prompts (`qa-2`, `longctx-1`)
by a plain substring check against `expected_answer`.

### Full experiment record

```sh
python3 llama/bakeoff/run_harness.py full \
  --alias nemotron-4b --experiment-id qwen36-35b-a3b-q4-xl-offload-b-100k-f16 \
  --hf-repo unsloth/Qwen3.6-35B-A3B-GGUF --hf-revision main \
  --gguf-host-path /models/unsloth/.../Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
  --context-ceiling 131072
```

Runs the sweep, both needle probes (moderate always; near-ceiling only if `--context-ceiling` is
given), and the quality corpus, then writes
`llama/bakeoff/records/<experiment-id>/{config.json,samples.jsonl,prompts.jsonl,completions.jsonl}`.

`--gguf-host-path` takes a **real host filesystem path** (not the container-internal `/models/...`
path baked into `llama/config.yaml`'s `cmd:` blocks) — this module deliberately does not hardcode
the `/models` → host bind-mount mapping, since that mapping lives in
`fleet-tools/compose.fleet.yml`, outside this repo's own contract. The caller (a later WP that
already knows the host layout from its own download step) resolves it. When given, `file_size`
and `sha256` are computed by reading the file directly (verified live during WP-1's dry run: a
2.8 GB GGUF hashes in about 1.3s — cheap enough to always compute for real rather than fake).
`hf_revision` falls back to `"unknown"` if not given (a local file has no directly-resolvable
commit SHA without a network call, which this WP does not make).

**Size budget**: before writing anything, `write_experiment_record` computes the total projected
on-disk size of the four output files. If it would exceed 25 MiB, it raises
`SizeBudgetExceeded` instead of writing a partial or over-budget directory — treat that as a
hold-for-user signal, not something to silently retry past.

### Live tokenizer calibration + calibrated needle probes

```sh
python3 -c "
from llama.bakeoff.run_harness import calibrate_chars_per_token
print(calibrate_chars_per_token('http://127.0.0.1:4811/v1', 'nemotron-4b'))
"
python3 llama/bakeoff/run_harness.py needle --alias qwen38-27b-iq3s --size moderate
# (target_actual_tokens is Python-API only -- not currently wired to the needle CLI subcommand)
```

`calibrate_chars_per_token(base_url, alias, probe_tokens=2000, timeout=60)` builds a modest
filler sample, fires one minimal chat-completion request (a small `max_tokens` -- only
`usage.prompt_tokens` is read back, the completion itself is discarded), and returns the
empirical ratio `real_prompt_tokens / probe_tokens` for that alias's real, live tokenizer.
**Live-derived every call — never a hardcoded constant.** `CHARS_PER_TOKEN_ESTIMATE` (the
module's ~4.5 filler-text sizing constant, used only to *target* a character count when building
filler) is not a substitute for this: a ratio this repo previously measured on one model at a
~35K-token sample is not guaranteed to hold for a different alias or a much larger (~150K-token)
target, which is exactly the gap this function closes. **If the calibration request itself errors
or times out, the function raises** rather than guessing a ratio — a calibration failure is a
hard stop for whatever called it, never silently defaulted to `1.0`.

`needle_probe(..., target_actual_tokens=100000)` (new, optional, additive parameter) uses this to
request a haystack sized to land at a *real* ~100K-token prompt, not a nominal character-count
guess that might resolve to something much smaller against an unfamiliar tokenizer. It calls
`calibrate_chars_per_token` first, builds and sends the haystack at the derived nominal size, then
checks whether the real `prompt_tokens` the server reports lands within **+/-10%** of
`target_actual_tokens`. If not, it rescales once more from the *observed* ratio of that very
attempt and retries **exactly once more** (never more than two total haystack-building requests
per call). The returned sample carries a new field, `calibration_tolerance_met: bool`, reflecting
whichever attempt was actually used (the second, if a retry happened) — `true` only if that
attempt's real `prompt_tokens` landed within the band, `false` otherwise; it is never silently
reported `true` when the band was missed. When `target_actual_tokens` is omitted (the default,
`None`), `needle_probe`'s behavior is byte-identical to before this parameter existed — the
existing `"moderate"`/`"near_ceiling"` size paths are untouched, and `calibration_tolerance_met`
is simply absent from the sample.

**Honest-timing-semantics note**: a calibrated target changes what gets *requested* (the nominal
haystack size sent to the server), **never** what gets *reported* — `prompt_tokens` in the sample
is always the real value read back from the server's own `usage` field for whichever attempt was
used, exactly matching this harness's existing contract (see above). Calibration makes the
*request* honest about what "~100K actual tokens" means for a given tokenizer; it does not change
how the *response* is recorded.

### Router log capture (router-managed aliases only)

```sh
python3 -c "
from llama.bakeoff.run_harness import fetch_router_logs
print(fetch_router_logs())
"
```

`fetch_router_logs(router_base_url=DEFAULT_ROUTER_BASE_URL, tail_bytes=None, timeout=30)` is a
thin wrapper around `GET {router_base_url}/logs` — llama-swap's own captured stdout/stderr log
ring buffer (confirmed live and returning `200` this session), reached via a **separate**
`DEFAULT_ROUTER_BASE_URL = "http://127.0.0.1:4811"` (no `/v1` suffix) rather than the chat-call
`DEFAULT_BASE_URL = "http://127.0.0.1:4811/v1"` — reusing the latter here would silently produce
the wrong path (`.../v1/logs`, which does not exist). `tail_bytes`, if given, trims the returned
string client-side to at most its last `tail_bytes` bytes.

**This is not a general process-log tool.** `/logs` captures stdout/stderr **only for children
llama-swap itself spawned** — it is the right mechanism for confirming a **router-managed**
alias's real startup output (e.g. that a landed KV-cache-type / Flash-Attention flag actually
took effect on the live process, not just in the config that requested it). It captures
**nothing** for a bare scratch `llama-server` process launched directly, outside llama-swap — a
different concern (`plans/qwen38-27b-kv-cache-quantization.md` WP-2 verifies its own scratch
server via a redirected process log instead, never via this function). Wiring `fetch_router_logs`
up against a scratch server will silently return the *router's* unrelated log content, not an
error — do not conflate the two.

## Sweep framework (`sweep.py`)

`plans/quant-sweep-harness-and-instrument-v2.md` WP-1. Turns "one model, one hand-typed `docker
exec`, one improvised candidate ladder" into a declarative spec plus automation for driving one
cell end-to-end.

### Spec format

```json
{
  "cells": [
    {
      "cell_id": "optional-explicit-override",
      "model_container_path": "/models/org/repo/file-Q4_K_M.gguf",
      "model_host_path": "/models/org/repo/file-Q4_K_M.gguf",
      "kv_attention_layers": 16,
      "alias": "candidate-alias",
      "server_flags": {"-c": 40960, "-ctk": "q4_0", "-ctv": "q4_0", "-fa": "on", "-ngl": 99},
      "battery": ["vram", "timing", "needle", "quality"]
    }
  ]
}
```

- `model_container_path` (required) — the path baked into the `-m` flag of the built command
  (this repo's `/models/...` container-internal convention, same split as `run_harness.py`'s
  `--gguf-host-path` vs. the config's `cmd:` block).
- `model_host_path` (optional) — a **real host filesystem path**. When given, the fit pre-filter
  reads it directly: `stat()` for `weights_bytes`, and a minimal GGUF header/metadata parse
  (`read_gguf_metadata`/`gguf_attention_geometry` — stdlib `struct`, no tensor data ever read)
  for KV-cache geometry (`block_count`, `head_count_kv`, `key_length`, `value_length`).
- `synthetic_geometry` (optional, mutually exclusive in practice with `model_host_path` — the
  latter always wins when present) — an inline override
  `{weights_bytes, block_count, head_count_kv, key_length, value_length, architecture?,
  head_count?, context_length?}` used when no real host file is available (this is what
  `examples/sweep-example.json` uses, so the dry-run gate never depends on a multi-GB model file
  being present on the host running it).
- `kv_attention_layers` (optional) — overrides the GGUF's raw `block_count` for the KV
  bytes/token formula. **Needed for hybrid architectures**: `docs/qwen38-27b-bakeoff-
  results.md:12-15` documents `qwen35`'s `full_attention_interval=4` (65 total blocks, only 16
  carry a real attention KV cache) — using raw `block_count` there overestimates KV bytes/token
  by ~4x. Verified live during this WP against the real `Qwen3.8-27B-UD-IQ3_S.gguf` header: the
  16-layer override reproduces the doc's own measured **0.0625 MiB/token** f16/f16 KV growth rate
  exactly; the raw `block_count=65` gives 0.254 MiB/token, ~4.06x too high.
- `kv_indexer_geometry` (optional, model-registry entry — WP-1, `plans/qwen4exp-bakeoff-
  campaign.md`, C4-B1) — `{"layers": int, "key_length": int, "value_length": int}` describing a
  SECOND, harness-invisible KV cache (a DSA indexer) some architectures allocate alongside the
  main attention cache — qwen4exp measured 36 MiB at 4096 cells, 37.5% on top of the modeled main
  cache. `gguf_fit.kv_bytes_per_token` adds a flat per-token rate on top of the main cache's when
  present. **f16-scoped**: nothing in this repo establishes the indexer cache honours the main
  cache's own `ctk`/`ctv` at a non-f16 KV type, so a cell carrying this field at a non-f16 KV type
  classifies `unknown` (`ValueError` inside `classify_fit`) rather than a guessed number — measure
  the indexer's storage type at that KV type first (a successor campaign's task, not this one's).
  Carried all the way to the persisted record (`cell_model.RecordMetadata`), appended after
  `spec_decode` alongside `gpu_weights_bytes`/`kv_attention_layers`/`swa_layer_spec`.
- `battery` — subset of `{"vram", "timing", "needle", "quality"}`.
- `cell_id` (optional) — explicit override for the deterministic per-cell ID; omit to have
  `compute_cell_id()` derive one from the cell's identity-bearing fields (never random, never
  timestamped — the same spec always reproduces the same IDs).

### VRAM-fit pre-filter

`classify_fit(cell, vram_profile="desktop-shared", vram_total_bytes_fn=..., vram_used_bytes_fn=
...)` returns one of `"fits"` / `"marginal"` / `"excluded"` / `"unknown"` (the last when neither
`model_host_path` nor `synthetic_geometry` is usable — never a guessed fit), plus the numbers
behind the classification. Formula:
`estimated_total = weights_bytes + kv_bytes_per_token(ctk, ctv, context=-c, ...) * -c +
NON_WEIGHT_OVERHEAD_GIB`, classified against `compute_vram_budget_bytes(vram_profile, ...)`
(below) with a `FIT_MARGIN_GIB`/`MARGINAL_MARGIN_GIB` band. Every constant is cited in
`sweep.py`'s own comments to a specific prior bake-off doc line or this plan's own text — see the
module for the full citations (card nameplate 15.92 GiB; the plan's own "~12.8 GiB realistic free
VRAM" sizing budget, `plans/quant-sweep-harness-and-instrument-v2.md:50-51`; ~456 MiB non-weight
overhead, `docs/qwen38-27b-bakeoff-results.md:177-179`; the "~1.5-2 GiB safety-margin convention"
repeated across every prior bake-off doc). **This is a pure-math predictor, never a substitute
for measurement** — the framework's house rule (plan Key Changes/WP-1): estimates gate what gets
attempted, a live VRAM read-back (`read_vram_used_bytes()`) is still what a real run trusts.

KV-cache byte-per-element table (`KV_TYPE_BYTES_PER_ELEMENT`) covers `f32`/`f16`/`bf16` (exact)
and `q8_0`/`q8_1`/`q5_0`/`q5_1`/`q4_0`/`q4_1` (ggml's own 32-element block-quant layout,
`block_bytes / 32`) — cross-checked against this repo's own live measurement in
`docs/qwen38-27b-bakeoff-results.md:158` (the q4_0 vs. q8_0 "KB/cell" comparison).

#### SWA-awareness (audit B2, `longcontext-pow2-overnight-campaign.md` WP-1)

Some architectures (gemma4) window-cap the majority of their attention layers — only a minority
of "global" layers actually scale their KV cache with `-c`. `gguf_attention_geometry` now also
extracts `<arch>.attention.sliding_window` (the window size) and `<arch>.attention.
sliding_window_pattern` (a per-layer bool array, `True` = window-capped) straight from the GGUF
header — live-verified against gemma-4-12B-it-qat-UD-Q4_K_XL.gguf's real header: 48 entries,
exact match against the `head_count_kv` array (40 SWA layers @ head_count_kv=8, 8 global layers @
head_count_kv=1, zero mismatches). `kv_bytes_per_token(geometry, ctk, ctv, context=..., 
swa_layer_spec=...)` uses this (or an explicit cell-level `swa_layer_spec` override, same shape)
to give window-capped layers `min(context, sliding_window)` tokens' worth of bytes and global
layers the full `context` — a ~18x reduction in gemma's predicted KV bytes/token at `-c 32768`
versus the old uniform-scaling formula, which is what made the earlier classifier wrongly exclude
gemma at every rung. **No heuristic**: absent an explicit per-layer signal (header or override),
this falls back to the pre-existing conservative (uniform-scaling) behavior — it never infers the
SWA/global split from `head_count_kv` magnitude alone.

#### Placement-awareness (audit B2)

A cell running under `-ncmoe` (or any other partial-offload flag) doesn't have the whole GGUF
resident on the GPU — counting the whole file overestimates VRAM pressure by the CPU-resident
fraction. A cell (or its queue model-registry entry) carrying `gpu_weights_bytes` has that figure
used in place of the raw file `stat()`/`synthetic_geometry.weights_bytes` size, applied uniformly
regardless of whether the geometry came from a real header or a synthetic fixture. qwen36-a3b's
`-ncmoe 32` config (`llama/config.yaml` offload-placement comments ~721-745): 2.494 GiB
non-expert weights (always GPU-resident) + 8 of 40 layers' routed-expert weights GPU-resident,
vs. its ~20.8 GiB whole-file size.

#### VRAM budget profiles (audit B4)

Two named profiles, never a bare constant — `compute_vram_budget_bytes(vram_profile,
vram_total_bytes_fn, vram_used_bytes_fn)`:

- **`desktop-shared`** — the pre-existing static `REALISTIC_FREE_VRAM_GIB` (12.8 GiB) figure.
  Touches neither VRAM reader at all (no live I/O for this profile — every existing dry-run/
  `classify_fit` caller is unaffected by this feature existing).
- **`headless`** — live: `min` isn't even needed, the formula is `mem_info_vram_total -
  measured_used_at_queue_start - HEADLESS_PINNED_MARGIN_GIB` (0.5 GiB). **Refuses**
  (`sweep.VRAMProfileRefused`) when the measured baseline exceeds `HEADLESS_BASELINE_CEILING_GIB`
  (1.5 GiB, pinned) — a live desktop session (measured ~3.1 GiB baseline, `llama/config.yaml`'s
  gemma fit-math comment) trips this refusal loudly rather than the profile silently
  overcommitting VRAM.

Both VRAM reader functions are injectable; the module-level `read_vram_total_bytes`/
`read_vram_used_bytes` are the real, live-capable sysfs-reading defaults — never invoked by this
repo's own test suite, same "real function exists, only ever invoked live" pattern as
`discover_bridge_ip`/`teardown_scratch_server` below.

### Ladder expansion

`expand_ladder(base_cell, kv_type_ladder, context_ladder, battery=None)` cross-products one base
cell against a `[(ctk, ctv), ...]` list and a `[-c, ...]` list, producing one normalized cell per
combination (alias gets a `-ctk<ctk>-ctv<ctv>-c<c>` suffix so cells never collide).

### Exact command building

`build_command(cell, port)` returns the exact `llama-server` argv a cell would run, in a fixed
flag order (`-m`, `--port`, `-c`, `-ctk`, `-ctv`, `-fa`, `-ngl`, then any remaining flags sorted)
— deterministic across calls, which is what `--dry-run`'s gate output relies on.

### Dry-run

```sh
python3 llama/bakeoff/sweep.py --dry-run llama/bakeoff/examples/sweep-example.json
```

Expands every cell, prints `cell_id`, `battery`, `server_flags`, the full `fit` classification
dict, and both the argv list and a shell-quoted `command_str` for each — as JSON to stdout.
**Touches no Docker, no network, no GPU.** The `--dry-run <spec>` spelling and the equivalent
`dry-run <spec>` subcommand spelling both work (the former matches this WP's gate invocation
literally).

### Scratch-server lifecycle (live path — `sweep.py run`, not exercised by WP-1 itself)

**Single implementation, reused by `batch.py`** (audit E1, `longcontext-pow2-overnight-
campaign.md` WP-1): the spawn → health-check → verify → teardown sequence below is implemented
once, as the `scratch_server(cell, port, container, health_timeout)` context manager — `sweep.py
run`'s own `cmd_run` and `batch.py`'s per-cell execution both go through it; neither
re-implements any piece of it. Entering the context manager spawns and health-polls; the yielded
`ScratchServerHandle` (`healthy`/`verified`/`details`/`log_text`/`base_url`/...) is what a caller
inspects (or, for `batch.py`, what its injectable `cell_runner` inspects/uses); teardown always
runs on exit, including when the caller's own code raises. `batch.py`'s tests inject a fake
context-manager factory in place of this — never Docker.

Container facts baked in (verified live 2026-08-23, this WP's brief): `inference-engine-llama`
has no `python3`, `kill`, or `pkill` inside the image; only the router's
4806(container)->4811(host) port is published; a scratch server's port is container-internal
only.

Two default `server_flags` are load-bearing, both first surfaced by the WP-1 live smoke cell
(2026-08-23): **`--host 0.0.0.0`** (llama-server defaults to 127.0.0.1 — container-internal
loopback — so the server comes up healthy while being unreachable via the bridge IP) and
**`-lv 9`** (the `llama_context`/`llama_kv_cache` lines the startup-state verification parses
never print at default verbosity, and *lower* `-lv` values print less, not more — without it
every cell is unverifiable-by-construction and classifies `invalid_config`). Both live in
`_DEFAULT_SERVER_FLAGS` with their citations; the verification patterns themselves are
live-verified against build `b10534`'s actual console format (see `_STARTUP_PATTERNS`).

- **Spawn**: `spawn_scratch_server(argv, log_path, container)` — `docker exec -d <container> sh
  -c 'nohup <argv...> > <log_path> 2>&1 < /dev/null &'`. `nohup`/`sh` are ordinary coreutils/dash,
  present in the runtime image; no `python3` needed to launch it.
- **Reach it**: `discover_bridge_ip(container)` — `docker inspect` for the container's bridge IP;
  the scratch server is reachable at `http://<bridge-ip>:<port>`, never `127.0.0.1:<port>` (that
  port isn't published to the host).
- **Health poll by response body** (root `AGENTS.md` Quirks: "Probe by response body, never
  status code"): `wait_for_health(base_url, timeout)` polls `GET {base_url}/health` and requires
  `body["status"] == "ok"` — a connection error, a non-JSON body, or any other status value keeps
  it polling, never counted as healthy from the status code alone.
- **Per-cell startup-state verification** ("a healthy endpoint has told you nothing" —
  `llama/AGENTS.md`): `verify_startup_state(log_text, cell)` regex-parses the scratch server's
  own redirected stdout/stderr and asserts the requested model path, `-c`, `-ctk`/`-ctv`,
  flash-attention state, and offload placement were **actually activated** — any field the log
  doesn't mention at all (e.g. `-v` wasn't passed, so `type_k`/`type_v`/`flash_attn` never print)
  counts as a mismatch, never a silent pass. A mismatch means the cell is `invalid_config` and
  its measurements don't count. These patterns are written against llama-server build `b10534`'s
  known console format but are **not live-verified by this WP** (no live Docker/GPU execution
  here) — the orchestrator's one live smoke cell (plan Test Plan) is where a pattern mismatch
  would first surface; a spurious `invalid_config` from a drifted log format is the intended,
  safe failure direction.
- **Teardown via host-visible PID**: `find_host_pid_by_port(port, container)` reads `docker top
  <container> -eo pid,args` (host PIDs, not container-namespace ones) and matches on `--port
  <port>`; `teardown_scratch_server(port, container)` signals that PID directly from the host
  (`os.kill` SIGTERM, then SIGKILL after a grace period if still alive) — never asks the
  container to kill its own child, since there's no `kill`/`pkill` binary inside the image to ask.
- **VRAM read-back**: `read_vram_used_bytes()` reads `mem_info_vram_used` directly from host
  sysfs (`/sys/class/drm/card1/device/mem_info_vram_used`) — this repo's established mechanism
  (see the Honest-timing-semantics-adjacent docs cited in `sweep.py`'s own comment). Read from
  the HOST path, not via `docker exec`: `/dev/dri` is passed through as the same physical device,
  so host and container see identical counters, and a host-side read works even before any
  container process exists to `exec` into. `teardown_scratch_server()` returns both a
  pre-teardown and post-teardown reading so a caller can confirm VRAM actually returned to
  baseline, not merely that the process is gone.

## Batch runner (`batch.py`)

`plans/longcontext-pow2-overnight-campaign.md` WP-1. Turns a self-contained queue file into an
unattended, resumable run: cells execute serially through `sweep.py`'s `scratch_server()`
lifecycle (never the router), with a durable state machine, a GPU-ownership maintenance window,
a kernel-log hang safety rail, and skip propagation for KV arms that fail correctness. **No live
operation happens by default** — every collaborator that could touch Docker/GPU/sysfs
(`ContainerController`, the real `scratch_server`, the real VRAM readers, a real hang-check
function) is injectable, and this module's own test suite (`tests/test_batch.py`) always injects
fakes.

### Queue file format (`queue_format: 1`, JSON)

**Self-contained** (audit E3, pinned): a queue never references an external registry file — cells
resolve against the queue's own `models:` map, so one queue file is one reproducible artifact.

```json
{
  "queue_format": 1,
  "vram_profile": "desktop-shared",
  "deadline": "13:00",
  "stop_policy": "pause-at-deadline",
  "models": {
    "gemma-4-12b-qat": {
      "model_host_path": "/models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",
      "model_container_path": "/models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",
      "server_flags": {"--jinja": true},
      "model_family": "gemma4",
      "chars_per_token": 4.3
    }
  },
  "cells": [
    {
      "model": "gemma-4-12b-qat", "alias": "gemma-4-12b-qat-c32768-f16",
      "c": 32768, "ctk": "f16", "ctv": "f16", "fa": "on",
      "battery": ["vram", "timing", "needle"], "sampling_arm": "capability", "trials": 3
    }
  ]
}
```

- `models` — model id → `{model_host_path, model_container_path, server_flags (base flags,
  e.g. `-ncmoe`/`--jinja`), gpu_weights_bytes (optional placement override), kv_attention_layers
  (optional), swa_layer_spec (optional), kv_indexer_geometry (optional — WP-1, see "Spec format"
  above), model_family (optional — production-sampling lookup), chars_per_token (optional —
  WP-6)}`.
- `chars_per_token` (optional, positive number) — the model family's MEASURED haystack-sizing
  ratio, replacing the static `CHARS_PER_TOKEN_ESTIMATE` (4.5) for this model's needle cells.
  WP-5's attended run measured the static estimate oversizing real Gutenberg-corpus prompts by
  +11–17% per family (nemotron hit ~0.99×`-c` at 64k; at 512k that overshoot would overflow the
  context). Values are derived from measured `prompt_tokens / target_tokens` distributions in
  committed records (midrange-centered so the observed band sits inside verify_run's ±10%
  tolerance): `llama/bakeoff/records/wp5-validation` gives gemma4 4.3, bailingmoe3 4.2, nemotron_h 4.17;
  qwen35moe is unmeasured and starts at a conservative 4.0. Deliberately NOT part of the cell
  identity: adding or tuning it never changes any `cell_id`.
- `cells` — an ORDERED list of abbreviated cells `{model, alias, c, ctk, ctv, fa, battery,
  sampling_arm, trials, spec_decode (optional — WP-2)}`. `resolve_cell()`/`resolve_queue_cells()`
  merge each abbreviated cell's `c`/`ctk`/`ctv`/`fa` onto its model's base `server_flags`,
  producing the normalized cell shape `sweep.py` expects (`normalize_cell`'s contract — see "Spec
  format" above), and compute each cell's `cell_id` (`sweep.compute_cell_id`); two queue entries
  resolving to a colliding `cell_id` is a validation error, never a silent merge.
- `spec_decode` (optional per-cell string, `"base"` | `"mtp"` — 2026-08-25,
  `plans/overnight-test-matrix-redesign.md` WP-2) — the base-vs-MTP axis. `resolve_cell` applies
  it to the merged `server_flags` BEFORE `normalize_cell`/`compute_cell_id`, so a base arm and an
  mtp arm of the same served config resolve to distinct `cell_id`s (identity is a hash over
  `server_flags`, among other things — see `compute_cell_id` above): `"mtp"` requires the cell's
  model registry entry to already carry `--spec-type` in its base `server_flags` — `resolve_cell`
  raises `QueueValidationError` ("a cell cannot ask for MTP a registry entry doesn't define")
  when it doesn't; `"base"` strips `-md` and `--spec-type` from the merged flags, so a queue can
  run the "no draft sidecar" comparison arm against an MTP-serving model's own registry entry,
  with no second model entry needed. **Absent is the default and every pre-WP-2 queue file's only
  state** — "registry `server_flags` as-is", byte-identical behavior; `queue_format` stays 1.
  An undeclared cell is no longer recorded blind, though (`plans/served-recipe-p50-prefill-and-decode.md` WP-2): `resolve_cell` stamps `spec_decode` `"mtp"` when the resolved `server_flags` carry `--spec-type` or `-md` and the queue declared nothing, `null` otherwise -- `"base"` is never inferred, only ever written from an explicit queue declaration.
  `validate_queue` rejects only a malformed PRESENT value (anything other than `"base"`/`"mtp"`,
  including an explicit JSON `null`) — the MTP-flags assertion needs the model registry, so it
  lives in `resolve_cell` instead. Recorded verbatim as `sweep_cell.spec_decode` in the cell's
  `config.json` (`None` when the cell never declared it) — schema-v2 additive, no version bump.
- `vram_profile` — `"desktop-shared"` or `"headless"` (see VRAM budget profiles above); the CLI's
  `--vram-profile` flag, if given, overrides the queue file's own value.
- `request_timeout_s` (optional, top-level, positive integer seconds — WP-1, `plans/qwen4exp-
  bakeoff-campaign.md`, cycle-2 blocking 1) — overrides the per-recv transport socket timeout
  (`harness_transport.py`'s `urlopen(..., timeout=...)`) that both the needle battery and the
  timing rounds run under; llama-server sends no bytes during prefill, so a slow-prefilling model
  at long context needs far more than the 300 s factory default. **Absent → 300 s**, so every
  pre-WP-1 committed queue is bit-identical in effective behavior. Not a `cell_id` input — it is
  **snapshotted into the queue state** at creation (`queue_state.init_state`) and compared on
  resume: a queue file edited after its state exists (changing only this field) raises
  `QueueValidationError` rather than silently resuming under a different timeout than the one the
  state file's evidence was captured under; a state file written before this key simply lacks it,
  and absence reads as the same 300 s default.
- `deadline` — an `"HH:MM"` **UTC** wall-clock cutoff (WP-5 finding #4: `run_queue`'s clock is
  `datetime.now(timezone.utc)`, so a deadline authored in local time fires hours off — this
  host is `America/Mexico_City`, permanent UTC-6 with no DST since 2022, so 07:00 local is
  always `"13:00"`). Recomputed fresh relative to EACH
  invocation's own start time (`compute_deadline_dt`): the next occurrence of that time at or
  after the invocation started (today if still ahead, tomorrow if already passed today) — an
  overnight queue spanning multiple nights gets a fresh deadline each night, by design.
- **A queue file is immutable once its state file exists.** Cell identity is the `cell_id` hash
  over the resolved flags, so ANY edit that adds/removes a cell or changes a server flag makes
  the queue and its sibling state disagree; `batch run` then refuses with a
  `QueueValidationError` naming the divergent cell_ids (WP-6; previously a bare `KeyError`).
  Move an edited queue to a new file (fresh state) or delete/archive the stale state file
  deliberately.
- `stop_policy` — currently only `"pause-at-deadline"` is implemented (validated, not just
  accepted-and-ignored) — a required, explicit field so a queue author never wonders what happens
  at a missed deadline; reserved for a future hard-stop policy without a queue-format bump.

### Durable state machine (audit B6)

State file: JSON, sibling of the queue file by default (`<queue>.state.json` —
`default_state_path`), atomically written (`save_state`: temp file + `os.replace`) after every
transition. Per-cell states: `pending` / `running` / `completed` / `failed` /
`skipped-propagated` / `aborted-hang`. Queue-level states: `running` / `paused-resumable` /
`completed` / `aborted-hang` (the last two are terminal — `TERMINAL_QUEUE_STATES`).
Each cell entry also carries the cell's `alias` at its top level (written by `init_state`,
preserved by every transition — `bench-dashboard-run-context-surfacing` plan) so external
readers (the Bench Dashboard's read-only `GET /api/queue`) can join queue cells to state
entries without reimplementing the `cell_id` hash; state files predating the field stay
valid (never required on read — such cells simply render unjoined).

- **Past the deadline**: no new cell launches; queue → `paused-resumable`. Already-running or
  already-terminal cells are untouched; every still-`pending` cell stays `pending` for the next
  invocation.
- **Terminal no-op**: `batch run` against a queue whose last-saved state is `completed` or
  `aborted-hang` prints a clear message and exits `0` without touching the container, the state
  file, or any cell — UNLESS `--re-arm` is passed.
- **`--re-arm`**: every cell not already `completed` (i.e. `failed`, `skipped-propagated`,
  `aborted-hang`, AND `pending`) resets to `pending`; queue state resets to `running`. A full
  retry of everything that didn't already succeed — a `completed` cell's record is never
  re-run.
- **`--re-arm-cell <cell_id-or-alias>`** (repeatable, WP-6): the targeted counterpart — resets
  exactly the named cells to `pending` (including a `completed` cell an operator explicitly
  names) and the queue state to `running`, leaving every other cell untouched. A name matching
  neither a state `cell_id` nor a queue `alias` is an error, never a silent no-op. This is the
  first-class version of the surgical state-file hand-edit WP-5's attended run performed three
  times.
- **Interrupted cell**: a cell found `running` at the START of an invocation means the previous
  invocation crashed mid-cell (a clean exit always leaves a cell in a terminal per-cell state) —
  it's marked `failed`/`interrupted` immediately, before anything else happens, and is never
  silently re-run in the same invocation (only a later `--re-arm` retries it).
- **Resuming a `paused-resumable` queue**: an ordinary `batch run` (no flags needed) picks up
  every still-`pending` cell in queue order; `completed`/`failed`/`skipped-propagated` cells are
  untouched.

### GPU ownership / maintenance window (audit B3)

`batch run` (non-dry-run) stops the `inference-engine-llama` container for the whole queue
lifetime and restarts it on every exit path — normal completion, a deadline pause, a hang abort,
and any exception (including from a `cell_runner`) — via a `try/finally` PLUS installed
`SIGTERM`/`SIGINT` handlers (`_install_signal_traps`, `_GracefulExit`) so an external signal is
just as `finally`-catchable as any other Python exception ("trap-guaranteed" per the plan). A
failed restart attempt is swallowed (logged nowhere yet — WP-4/WP-6 territory) rather than
masking the original failure or blocking process exit. `ContainerController` is the real
`docker stop`/`docker start` collaborator — constructed only by `cmd_run` for a live, non-dry-run
CLI invocation; `run_queue` itself just calls whatever object it's given (or does nothing if
`None`), so no test in this repo ever invokes it.

### Unattended safety rail — kernel-log hang signature

`detect_hang_signature(lines)` matches EITHER `"MES might be in unrecoverable state"` or
`"Failed to evict process queues"` (verbatim from `fleet-tools/references/rdna4-gpu-
hang.md`'s "The signature" section and `fleet-tools/scripts/gpu-hang-watchdog.sh`'s own
`SIGNATURE_RE` — either line alone is sufficient; journald delivers lines one at a time, so the
full cascade never arrives as one matchable unit). `run_queue`'s injectable `hang_check_fn`
(default a no-op) is checked once per cell; a match — or a `cell_runner`-reported `"aborted-
hang"` status — marks that cell `aborted-hang`, halts the QUEUE terminally (`aborted-hang`, no
further cells run, none get marked `skipped-propagated` — the queue just stops), and the
maintenance-window restart above still fires. `poll_journal_for_hang(since_iso)` is the real,
live-capable `journalctl -k --since <since>` polling implementation (repeated polling with an
advancing `--since` achieves the same never-miss-a-line property as a persistent `-f` follower,
without a background-thread/subprocess-lifecycle to manage) — never invoked by this repo's own
tests, same pattern as `ContainerController`.

### Skip propagation (audit E2, pinned)

When a cell whose KV arm is quantized (`-ctk`/`-ctv` != f16) terminates `failed` for a
CORRECTNESS reason (`CellOutcome.correctness_failure=True` — reserved for a classifier
`degenerate` verdict or a needle-recall failure, never for health-timeout/OOM/interrupted/
size-exceeded), `propagate_skips()` marks every LATER `pending` cell (queue order) with the same
`(model, ctk, ctv)` and a strictly larger `-c` as `skipped-propagated`, referencing the failing
cell's id. A non-correctness failure (health timeout, OOM-refusal) never propagates — the next
cell just runs normally. Deterministic, unit-tested.

### Pre-enqueue size check (audit E4, pinned)

Before launching any cell with `-c >= 262144`, `project_cell_record_size_bytes(cell, trials)`
builds a synthetic WORST-CASE sample+completion pair (every string field at a conservative
maximum length, matching the real `needle_probe`/`longcontext.py` record shape), serializes it
exactly as `run_harness.write_experiment_record` would, multiplies by `trials` then by 1.25
(headroom), and compares against `run_harness.SIZE_LIMIT_BYTES`. Over budget → the cell is marked
`failed`/`projected_size_exceeded` WITHOUT ever spawning a scratch server for it; the writer's own
authoritative pre-write guard (`run_harness.py` ~1106-1189) is untouched and still the last word
for whatever actually gets written.

### Battery-execution seam (deferred to WP-2)

This WP owns the queue/state-machine/safety-rail scaffolding, not the actual longcontext battery
run. `run_queue`'s `cell_runner` parameter is the seam WP-2 wires the real battery into (per that
plan's Key Changes: "`llama/bakeoff/batch.py` (battery hookup)"); `default_cell_runner` here is a
placeholder that only checks scratch-server startup verification (`ScratchServerHandle.healthy`/
`.verified`) and marks the cell `completed` on that basis alone — it runs no battery at all. This
WP's own tests inject scripted fake `cell_runner`s to exercise every disposition (completed /
health-timeout / OOM / degenerate / needle-recall failure / hang) the state machine has to
handle, without the real battery existing yet.

### The `timing` battery item (2026-08-25, `plans/bakeoff-timing-records-and-estimates.md` WP-1)

`"timing"` has been in `sweep.VALID_BATTERY_ITEMS` and in live queue files since long before it
did anything: a `battery: ["needle","timing"]` cell used to run only its needle arm, silently.
It now runs a small round pass whose `timing_round` rows are appended to the same record's
`samples.jsonl` (see the 2026-08-25 additive-field note in "Schema v2" above).

- **`run_harness.run_round` is reused verbatim**, not reimplemented — it already emits exactly
  the `timing_round` shape the dashboard parses, and it already never raises on a transport
  failure (it records `success: false` on the row instead).
- **3 rounds by default** (`round_index` 0–2), overridable per cell with an optional integer
  `timing_rounds` key on the queue's abbreviated cell — validated at queue-load time, and set on
  the resolved cell AFTER `compute_cell_id`, so measurement depth never perturbs cell identity
  (same contract as `chars_per_token`).
- **Round 0 is the `cold` round; rounds 1+ are `hot`** and resend the identical short prompt, so
  `run_round`'s real cache-evidence assertion (`cached/prompt > 0.90`) actually means something
  for them. The prompt is short on purpose: this arm measures decode throughput and first-token
  latency, while the needle arm is what exercises long context.
- **The timing pass runs FIRST** for a cell that has both `"timing"` and `"needle"`. That
  ordering is the whole basis for calling round 0 cold — nothing else has touched the scratch
  server since startup verification.
- **`model_load_seconds` stays `null` on every row this arm writes**, by the honest-timing
  contract: a scratch server is a bare `llama-server` with no llama-swap `/models` residency
  status to poll, and it was already health-checked and startup-verified before round 0, so there
  is no residency transition left to observe. The honest number for that cost is
  `config.json`'s `scratch_startup_seconds`. Nothing subtracts an assumed constant to fill the
  gap; a reviewer should revert any change that does.
- **The timing arm never changes a cell's disposition.** `CellOutcome.status`/`reason`/
  `correctness_failure` are decided by exactly the needle ladder documented above, over exactly
  the needle battery's samples; the timing pass reports itself under `detail["timing"]`
  (`rounds_requested`/`rounds_recorded`/`rounds_succeeded`/`error`) and a failed round shows up
  as `success: false` on its own row. A cell WITHOUT `"timing"` in its battery is unchanged apart
  from the additive `config.json` keys.

### Timing depth/length knobs and `timing_geometry` (2026-09-03, `plans/small-dense-decode-roofline-spike.md` WP-1)

Two additive, non-identity abbreviated-cell keys extend the arm above. `timing_prompt_tokens` (int, optional) swaps the fixed ~87-character `TIMING_PROMPT` for `harness_fixture._build_filler_text(target, seed=FIXTURE_SEED)` (reused verbatim, never reimplemented) plus a pinned, cap-exhausting continuation instruction (`"\n\nContinue the text above in the same style for as long as you can, without stopping or summarizing."` -- distinct from `FIXTURE_INSTRUCTION`, which asks a bounded, self-terminating question); `timing_max_tokens` (int, optional) overrides `TIMING_MAX_TOKENS` (64). Both validated like `timing_rounds`, both resolved AFTER `compute_cell_id` (`cell_model.QueueCellMetadata`, not `CellIdentity`), so neither can perturb a cell's identity. Absent either knob, a cell's timing rounds are byte-for-byte what they were before this WP.

**Why depth matters.** The pre-existing fixed short prompt puts only tens of tokens on the wire (template-dependent), and `model_scoreboard`'s `local-capture` ingest buckets any record whose median prompt depth is `< 1000` tokens as `shallow` and discards it outright -- a shallow timing row can never enter that estimator's calibration set. `timing_prompt_tokens` is how a cell reaches deep-prompt territory (e.g. `8000`, matching the estimator's own calibration bucket) without touching the `"needle"` battery item's long-context machinery at all -- the two arms stay independent knobs on the same scratch server.

**`timing_geometry` on the record.** When at least one knob is set, `write_cell_record` stamps a new optional `config.json` key, `timing_geometry: {prompt_tokens_target, max_tokens, prompt_source: "fixed-short"|"fixture-filler", instruction_pinned}` -- **never** `timing_summary`: that name is reserved by the scoreboard's `capture.ts` legacy-v1 classifier and would cause every v2 timing sample in the record to be silently discarded. A cell that sets neither knob gets no `timing_geometry` key at all (omitted, not `null`), so its record stays byte-identical to one written before this WP.

**Sampling is server-default for timing-only cells.** `run_timing_rounds` (and `run_round` underneath it) sends no sampling arguments, so a cell whose battery has `"timing"` but not `"needle"` never resolves its `sampling_arm` request at all -- `_startup_only` persists with `sampling_arm_effective=None`, so the queue's mandatory `sampling_arm` key is schema-satisfying only for such a cell, and its record's `sweep_cell.sampling_arm` is `null`, never the requested arm.

### CLI

```sh
python3 llama/bakeoff/batch.py run --queue <queue.json> [--vram-profile desktop-shared|headless] [--dry-run] [--re-arm] [--re-arm-cell <cell_id-or-alias>]... [--scratch-container <name>] [--records-dir <run-dir>]
python3 llama/bakeoff/batch.py verify --run <run-dir>
```

With `--records-dir`, each live cell's persisted record `config.json` carries two additive
self-description keys (`bench-dashboard-run-context-surfacing` plan; `schema_version` stays 2):
`vram_profile` — the effective `--vram-profile`/queue value the cell ran under — and
`response_caps` — `{"default": <flat max_tokens>, "by_task": <LONGCONTEXT_MAX_TOKENS_BY_TASK>}`,
the response-length budget the battery actually enforced. Both `null` on records written by
callers that don't supply them; absent entirely on records predating the plan (never backfilled).

`batch run --dry-run` resolves every cell and prints its `classify_fit()` classification (same
shape as `sweep.py --dry-run`, one level up — queue-level, not spec-level) — touches no Docker,
no state file, no container. `batch verify` is a dispatch seam only in `batch.py` itself: it
tries `import verify_run` and delegates to its `main()`; absent that module it prints a clear
message to stderr and exits `2` — never a traceback. **WP-4 lands `verify_run.py`** (below), so
that absent-module path is now only reachable in a worktree/checkout that predates this section.

## Test matrix (`queues/matrix-v1-shared.json`, `queues/matrix-v1-dedicated.json`)

`plans/overnight-test-matrix-redesign.md` WP-3. Replaces the ladder queues with an axes-first pilot matrix: the user's direction (2026-08-25) named six axes — ceiling validation under shared/dedicated GPU, needle validation on quantized KV, timing (TTFT + t/s) on all measurements, long-context prefill and decode, base-vs-MTP, and "so on" — and each one maps onto an existing queue dimension rather than a new one (plan Decisions locked), except base-vs-MTP:

- gpu_mode → queue-level `vram_profile` (`desktop-shared` for Tiers A–D/F, `headless` for Tier E)
- context → `c`
- KV quantization → `ctk`/`ctv`/`fa`
- timing TTFT/t/s and long-context prefill/decode → the `"timing"` battery item's hot/warm `timing_round` rows plus the per-probe `server_prompt_eval_tok_s`/`server_eval_tok_s` on `"needle"` rows — at these tiers' 128k/256k prompt sizes those needle-row rates ARE the long-context prefill/decode measurement (real ~100k-token prompts, server-reported, never wall-derived)
- sampling → `sampling_arm` (`"capability"` on every tier except F's `"production"`)
- base-vs-MTP → the additive `spec_decode: "base" | "mtp"` cell key (WP-2), the one axis with no pre-existing dimension

Every cell in both files carries `battery: ["timing", "needle"]` and `trials: 3`; every alias is fresh and `mx-`/`mxd-`-prefixed (append-only alias contract — none reuses a ladder-v1/ladder-v2/wp5-validation alias).

### Tiers (18 cells `matrix-v1-shared.json`, priority-ordered A → B → F → C → D; 4 cells `matrix-v1-dedicated.json`, Tier E)

- **Tier A — ceiling validation at served config (9 cells, shared)**: every chat model at its exact live `cmd:` values in `llama/config.yaml` — the machine-readable record the live-landed prose ceilings never had. The two MTP-serving models (`gemma-4-12b-qat`, `qwen36-a3b-mtp`) carry explicit `spec_decode: "mtp"` so the recorded `sweep_cell.spec_decode` is self-describing on both comparison arms (audit E-3 pin); the other 7 cells leave the key absent (registry `server_flags` as-is).
- **Tier B — base vs MTP (2 cells, shared)**: `gemma-4-12b-qat` and `qwen36-a3b-mtp` at served config with `spec_decode: "base"` — Tier A is the MTP arm of the same comparison; needle guards the losslessness claim, timing quantifies the MTP win at real 128k context.
- **Tier F — production sampling arm (2 cells, shared)**: `gemma-4-12b-qat` and `qwen36-a3b-mtp` (families `gemma4`/`qwen35moe`) at served config with `sampling_arm: "production"`. Verified against `sampling_table.py`: both families carry a complete `{temperature, top_p, top_k}` production entry there (so does `bailingmoe3`/Ling and `qwen35`/dense-Qwen38 — this pruned pilot exercises the axis on two families, not the full cross-product). `nemotron_h` has no entry at all: its card states `temperature`/`top_p` per use-case but never states `top_k`, so per that module's do-not-invent contract it stays `capability`-only, in the pilot and always.
- **Tier C — needle on quantized KV (3 cells, shared)**: `nemotron-4b-ud`, `ling-3-tiny-ud-q4`, `gemma-4-12b-qat` at `c 131072`, `ctk`/`ctv q4_0`, `fa on` — the KV-quant recall counterfactual for the models this fleet serves f16 today (a VRAM-headroom win if recall holds). `qwen38`'s f16 counterfactual at 128k is analytically VRAM-impossible on this card (its f16 ceiling tops out at 40960/24576 depending on quant, far short of 131072 — quantized KV is why it reaches 131072/65536 at all) and is deliberately not authored.
- **Tier D — above-served long-context exploration (2 cells, shared, absolute tail)**: `gemma-4-12b-qat` and `nemotron-4b-ud` at `c 262144` f16 (served `fa` values) — ladder-v2's own 256k cells, carried over.
- **Tier E — dedicated-GPU re-run (4 cells, `matrix-v1-dedicated.json`)**: `gemma-4-12b-qat`, `qwen36-a3b-mtp`, `qwen38-iq3s`, `nemotron-4b-ud` at served config under `vram_profile: "headless"` — the shared-vs-dedicated ceiling/timing comparison, joined to Tier A by model plus the per-cell recorded `vram_profile`. `spec_decode` stays absent on every Tier E cell (the plan pins explicit values only on the Tier A/B comparison arms).
- **Tier E-prod — dedicated-GPU production-arm + reasoning-control variants (10 cells, `matrix-v1-dedicated-prod.json`, user direction 2026-08-26 after the Tier E results)**: every cell `sampling_arm: "production"` under `vram_profile: "headless"`, `mxdp-` aliases. Order as committed (reordered 2026-08-26 after the first window): four **timing-only** qwen36 cells (`battery: ["timing"]`, 1 trial — `spec_decode` mtp|base × thinking|`--reasoning off`, added after the Ling window to test whether the observed 53 → 37 t/s no-think decode drop is an MTP-acceptance effect or survives without the draft) → Ling UD-Q4/UD-Q6/Q8 → gemma-qat / qwen36 / qwen38-iq3s at served config → qwen36 + qwen38-iq3s with `--reasoning off` → the same two with `--reasoning-budget 128`. The first 360-min window (05:42Z, original order Ling-last) landed gemma-qat, qwen36, qwen38-iq3s and qwen36-nothink, then was cancelled mid-`qwen38-iq3s-nothink` for desktop use (SIGTERM to `batch.py` — the dashboard `cancel` route is pre-claim only; the interrupted cell was reset to `pending` in the state file, no records were written); Ling was pulled to the front for a same-day window, the long qwen cells run in the next overnight window. Terminal cells are skipped on resume regardless of position. The reasoning variants are registry entries (`*-nothink`, `*-rb128`) whose extra `server_flags` shape `cell_id` — no harness change. Only *lower* reasoning is testable this way: the battery's response cap is fixed per `batch run` (256 tokens, 768 for `multi_needle`) and llama.cpp's `max_tokens` counts thinking tokens, so a *higher* budget needs a queue-level response-cap key first (deferred; Tier E measured qwen36 hitting the cap in 17/27 probes with empty `content_text`). `nemotron-4b-ud` is deliberately absent — with no `sampling_table.py` production entry it would silently re-run the Tier E capability cell.
- **RAM A/B spike (`plans/qwen4exp-ram-ab-spike.md` WP-1, three queues, 5 cells each, `headless`)**: three byte-identical queues — the four `qwen4exp-viability-v1` cells plus the `mxdp-qwen36-c128k-f16-timing-mtp-think` GPU-resident control cell (control last, so a qwen4exp failure cannot be masked by it), `request_timeout_s: 1800` — that differ only in which host-memory state they are run under, the state carried by the file name and the measurement directory, never by content:
  - `queues/qwen4exp-ram-2dimm-jedec-v1.json` — the 2 × 32 GB pair at JEDEC 4800 MT/s, as installed today (EXPO off)
  - `queues/qwen4exp-ram-2dimm-expo-v1.json` — the same 2 × 32 GB pair with the kit's EXPO profile enabled in BIOS (no hardware change)
  - `queues/qwen4exp-ram-4dimm-v1.json` — the 2 × 32 GB pair plus a spare 2 × 16 GB pair installed in the board's empty slots

Fit-safety ordering (audit/fit-safety pin): Tiers A/B/E/F are exactly the live-verified serving configs (or strictly below — a base arm drops a draft sidecar), so they run first; Tiers C/D push past the served ceiling and rely on `classify_fit` plus scratch-server startup verification to fail fast, so they are ordered last within the shared queue — a `pause-at-deadline` truncation drops Tier D, then Tier C, and never touches Tier A.

### Dedicated-window operator recipe (WP-4, measured 2026-08-25)

`matrix-v1-dedicated.json` selects `vram_profile: "headless"`, which is fail-closed: at queue start `compute_vram_budget_bytes("headless")` live-reads `mem_info_vram_used` and raises `VRAMProfileRefused` when the baseline exceeds `HEADLESS_BASELINE_CEILING_GIB` (1.5 GiB) — a desktop session still holding the GPU can never run "dedicated" cells by accident. The spike measured whether a quiesced-but-logged-in desktop clears that bar, so the dedicated tier needs no logout and no mechanism redesign:

- **Measured baseline (quiesced desktop, logged-in seat0 session, no router model resident):** `mem_info_vram_used` = 376,111,104 B (0.350 GiB), stable across four samples over ~15 s (0.348–0.379 GiB); `mem_info_vram_total` = 17,095,983,104 B (15.922 GiB). `compute_vram_budget_bytes("headless")` **admitted** with a budget of 16,183,001,088 B (**15.07 GiB** = total − used − the 0.5 GiB pinned margin) — against the 12.8 GiB `REALISTIC_FREE_VRAM_GIB` figure the `desktop-shared` profile plans around, and the ~3.1 GiB baseline the same card shows with a working desktop (ghostty/firefox/LM Studio/obsidian/nvtop all holding the render node, `sweep.py`'s own fit-math comment). Only driver-level allocations remain at 0.35 GiB; the one user process still on the render node was `nvtop` itself.
- **Confirmed quiesce procedure (the user's choice: close GPU-holding apps, stay logged in):** close every app that holds `/dev/dri/renderD128` — browsers (Firefox was the last holdout in the spike, worth ~30 MB on its own), LM Studio, Obsidian, terminals with GPU-accelerated rendering — leaving the session itself up. Verify with `cat /sys/class/drm/card1/device/mem_info_vram_used` (expect < 1.6e9 B) and `curl -s http://127.0.0.1:4811/running` (expect `{"running":[]}`, i.e. no router model resident); the loader re-checks the same number at queue start, so a stale verification only costs a refused run, never a bad record.
- **Window sequence:** (1) campaign preflight — `systemctl is-active gpu-hang-watchdog.service` active+enabled, save-work hold; (2) quiesce as above and verify; (3) arm and start via the control plane with an explicit `start` intent carrying `queue_stem: "matrix-v1-dedicated"` (see "Scheduling" below — never rely on `cron-nightly.sh`'s default argument, which selects the shared queue); (4) the 4 Tier E cells run — every cell is a live-verified served config, so the window is bounded by roughly four 128k cells and the last-cell overrun past `deadline` is ≤ ~1.5 h by construction; (5) desktop returns after the run's terminal state — reopening GPU-holding apps mid-window does not abort a running cell but would refuse the next queue start, so it simply wastes the slot.
- **Per-cell record join:** each Tier E record carries the effective `vram_profile: "headless"` plus its own `vram.baseline_bytes`, so the shared-vs-dedicated comparison against Tier A is on model + `vram_profile`, with the measured baseline preserved per record rather than assumed from this section.

### Ladder-v2 deletion

`llama/bakeoff/queues/ladder-v2.json` is deleted, un-run (user decision 2026-08-25, choosing deletion over supersede-in-place, `plans/overnight-test-matrix-redesign.md` WP-3): it carried 11 cells over the pre-matrix landed recipes, no `.state.json`, and no `llama/bakeoff/records/ladder-v2/` directory ever existed, so nothing is lost. Its 9-model registry and 256k cells are this matrix's direct ancestor (Tier A/D lift its `models` map and carry its two 256k cells forward). Its own 11 aliases (`gemma-qat-c128k-f16-mtp` and siblings, none `mx-`/`mxd-`-prefixed) retire unused but stay reserved under the append-only alias contract — no matrix cell, present or future, may reuse one. Two live-state references to ladder-v2 went stale on deletion and were fixed in the same change: `cron-nightly.sh`'s default queue argument (now `matrix-v1-shared.json`) and `docs/gfx1201-kv-fa-mtp-findings.md`'s "committed DISARMED" sentence.

Not authored in this pilot (plan Assumptions): vision probes, tool-calling under load, and a quality-corpus battery — follow-on tiers for a future `matrix-v2` queue once a `quality` battery item exists.

## Small-dense decode roofline spike (`queues/small-dense-roofline-v1.json`)

`plans/small-dense-decode-roofline-spike.md` WP-1. Twelve scratch cells (no router alias, no `llama/config.yaml` entry) at 8000-token prompt depth / 256-token cap / 3 rounds (`timing_prompt_tokens`/`timing_max_tokens`, see the timing-arm subsection above), measuring decode tok/s across the Qwen3.5-4B and Qwen3.5-0.8B quant ladders (`unsloth/*-GGUF`, BF16/Q8_0/Q6_K/Q4_0) and MiniCPM5-1B (`openbmb/MiniCPM5-1B-GGUF`, F16/Q8_0/Q4_K_M), plus one matched control cell against the file the live `nemotron-4b` router alias actually serves. `battery` is `["vram", "timing"]` only (no `"needle"`) -- this queue exercises the depth/length knobs without the long-context instrument. Aliases (append-only reserved, `sdr-`-prefixed, one alias per model registry key): `sdr-qwen35-4b-{bf16,q8,q6k,q40}`, `sdr-qwen35-08b-{bf16,q8,q6k,q40}`, `sdr-minicpm5-1b-{f16,q8,q4km}`, `sdr-nemotron-4b-udq4xl-ctl` — no cell, present or future, may reuse one.

## Resident Qwen3.6 MoE decode spike (`queues/qwen36-resident-v1.json`)

`model-scoreboard/plans/hardware-pareto-resident-moe-measurement-quant-subset-and-sibling-highlight.md` WP-0. Two scratch cells (no router alias, no `llama/config.yaml` entry) measuring FULLY-RESIDENT decode of Qwen3.6 35B A3B (`unsloth/Qwen3.6-35B-A3B-GGUF`, UD-IQ3_XXS 13.2 GB and UD-IQ2_M 11.5 GB — both fit the 16 GiB RX 9070 with f16 KV over 10 attention layers at 16k) at the same 8000-token / 256-cap / 3-round timing geometry as `small-dense-roofline-v1`. Deliberately `-ngl 99 --jinja` only: no `-ncmoe` (nothing offloaded), no `--spec-type`/`-md` (MTP would measure speculative acceptance, not the base decode roofline the scoreboard's `throughput.ts` estimator predicts). The scoreboard's `local-capture` source ingests the two records onto the family's existing `ud-iq3-xxs` / `ud-iq2-m` quantization artifacts, giving `calibrate:local` its first MoE-resident cells outside Ling 3.0 Tiny. Aliases (append-only reserved, `q36r-`-prefixed): `q36r-ud-iq3-xxs`, `q36r-ud-iq2-m` — no cell, present or future, may reuse one.

## Scheduling -- host control plane (`control_dispatcher.py`, `run-window.sh`)

`plans/bakeoff-queue-control-plane.md` WP-3. Files-only, minutely host dispatcher, no persistent
supervisor daemon: a dashboard/API-written drop-box of intent files is turned into effects and
durable evidence by `control_dispatcher.py dispatch`, a one-pass-per-invocation cron job. The
GPU-owning run itself (manual or nightly) is executed by `run-window.sh`, extracted from the
former (fatter) `cron-nightly.sh`, which is now a thin arm-gated shim over it. **No live
Docker/GPU operation happens in this module's own test suite** -- every process/Docker-touching
collaborator (`launch_fn`, `find_pid_fn`, `teardown_fn`, `router_start_fn`, `probe_cron_lock_fn`,
`boot_id_fn`, `pgid_alive_fn`) is injectable, same pattern as `batch.py`/`sweep.py`.

### State-dir layout

```
~/.local/state/inference-bakeoff/       (override: env BAKEOFF_STATE_DIR / --state-dir)
  cron.lock                      # THE run lock (admission); flock target for run-window.sh
  dispatch.lock                  # dispatcher self-serialization (hygiene, not admission)
  control/
    requests/                    #   drop-box: mounted rw in the dashboard container
      <request-id>.json          #     intent, written ONCE by the API -- read-only here
    stop-requested.<inv-id>      #   target-bound stop marker
    host/                        #   host-written; mounted ro in the dashboard container
      armed                      #     marker file: presence = nightly armed (fail-closed)
      claims/<request-id>.json   #     durable claim, written BEFORE launch
      results/<invocation-id>.json # wrapper-owned launch/exit evidence
      actions/<request-id>.json  #     pre-effect evidence for stop/cancel
      dispositions/<id>.json     #     terminal request record (retained indefinitely)
      live.json                  #     wrapper-published liveness + phase
      last-run.json              #     dispatcher-composed (sole writer)
      status.json                #     dispatcher digest each pass -- the API's read surface
```

The queues dir (`llama/bakeoff/queues/`, used to validate a `start` request's `queue_stem`) is
also overridable (env `BAKEOFF_QUEUES_DIR` / `--queues-dir`) so every test in
`tests/test_control_dispatcher.py`/`tests/test_run_window*.py` runs entirely inside a tempdir --
never the real state dir above.

### Dispatcher cadence and the six phases

`control_dispatcher.py dispatch` is intended to run every minute under `flock -n dispatch.lock`
(self-serialization hygiene only -- if a prior pass is still running, the new one quietly skips;
this is NOT the admission mechanism). Each pass, in order: (1) **ingest** -- hardened, fail-closed
parsing of the drop-box (basename grammar, `O_NOFOLLOW` open, an 8 KiB size bound, body/basename
id agreement, schema validation); a non-matching basename (including the API's own transient
`.`-prefixed `*.tmp` exclusive-creation files) is ignored and counted, never dispositioned; a
matching-but-invalid request is dispositioned `rejected(malformed)`; (2) **act** -- `arm`/`disarm`
flip the `armed` marker; `stop` and `cancel` write pre-effect evidence before their effect, and a
crash mid-act resumes from that recorded evidence on the next pass rather than re-deriving
anything; `start` probes `cron.lock` non-blocking, writes a claim, and detach-launches
`run-window.sh` via `setsid`; (3) **terminalize from evidence** -- a claimed request only becomes
`launch-failed` once the evidence (or its absence past a 5-minute grace) says so, never from bare
elapsed time; (4) **liveness/janitor** -- process-group truth (a wrapper pid alone is never run
liveness; `batch.py` survives as a detached child by design), with workload-aware crash recovery
(orphan scratch-server teardown, then router restart, then `last-run.json` composed `crashed`,
then `live.json` removed); (5) **publish `status.json`** (atomic temp+rename) -- the exact shape
is pinned by the LANDED dashboard API reader (`dashboard/apps/api/src/routes/queue.ts`
`buildControlSection` + `dashboard/packages/shared/src/control.ts`); (6) **prune** -- drop-box
requests/results/actions older than 30 days, and stop markers naming no-longer-live invocations;
`claims/` and `dispositions/` are retained forever (the audit record).

### Arm/disarm semantics

The nightly is **fail-closed**: `cron-nightly.sh` checks for `control/host/armed` and exits 0
(logging `disarmed`) without touching Docker or the queue if it's absent. Only the dispatcher
writes/removes that marker, driven by an `arm`/`disarm` control request (`POST
/api/control/{arm,disarm}` once the dashboard is wired, or a hand-placed request file for manual
testing). Disarming mid-run has no effect on an already-live run -- the gate is invocation-time
only; `stop` is the tool for a live run.

### The crontab lines (installed by the operator, currently DISARMED)

Two lines. The **dispatcher** runs minutely -- it is the control plane's only pump (ingest,
act, terminalize, janitor, status publish, prune); without it, requests sit `pending` forever
and `status.json` goes stale (>180 s triggers the dashboard's control-offline banner). It
self-serializes with a non-blocking `flock` on `dispatch.lock`, so overlapping firings are
harmless. The **nightly** keeps the pre-control-plane convention (`llama/README.md`'s own
Scheduling section) but **drops its own outer `flock`**: the wrapper now owns `cron.lock` for
the run's whole process-group lifetime, so a second `flock` around the crontab line would just
be redundant serialization at the wrong layer.

```cron
* * * * * python3 llama/bakeoff/control_dispatcher.py dispatch >> ~/.local/state/inference-bakeoff/dispatch.log 2>&1
0 23 * * * llama/bakeoff/cron-nightly.sh >> ~/.local/state/inference-bakeoff/cron.log 2>&1
```

Installing these lines is WP-6's job, done with the user present -- this repo never installs a
crontab on its own. Until WP-6 lands them, the nightly simply never fires; once installed, it
stays governed by the `armed` marker above (currently absent -- nothing runs until an explicit
`arm`). The dispatcher line is safe to run while disarmed: with no requests and no live run,
a pass only refreshes `status.json`.

### Manual-window flow

A manual start (`POST /api/control/start {queueStem, minutes}` once the dashboard API is wired,
or a hand-placed `req-<id>.json` request) is picked up by the next dispatcher pass: claimed, then
`run-window.sh <queue> --minutes <N> --request-id <request-id>` is detach-launched via `setsid`.
`--minutes` becomes `batch.py run --deadline-minutes N` -- an invocation-scoped override of the
queue file's own `HH:MM` deadline; the queue file itself is never rewritten. A `stop` request
against a verified-live run creates a target-bound marker (`stop-requested.<invocation-id>`) that
the wrapper clears at its own next window start and `batch.py` consumes at the next cell boundary
(pause-resumable) -- delivered, not necessarily honored immediately: "effect is
pause-after-current-cell, or a no-op if the run completes first."

### Crash-recovery behavior an operator sees

Operator-visible truth keys off **verified liveness first, queue state second** (queue state is a
*disposition*, never liveness). A live, verified run shows `running` regardless of what the queue
state file happens to say (a `paused-resumable` queue with a verified-alive process group is
still shown `running` -- liveness wins). A dead process group with `live.json` still present is
recovered by the janitor -- surviving scratch-server workload torn down, router container
restarted, then `last-run.json` composed `disposition:"crashed"` and `live.json` removed; the next
window's own interrupted-cell rule marks the in-flight cell `failed`/`interrupted` and NEVER
silently re-runs it. A wrapper that dies while its `batch.py` child is still running is shown
`running (wrapper lost)` -- an anomaly, not a crash; `stop` stays admissible since `batch.py`
(the marker consumer) is the survivor. `last-run.json`'s own `disposition` is `clean` (both exit
codes 0 -- a deadline/stop pause is a normal exit-0 outcome, not an attention condition on its
own), `needs-attention` (any other exit-code combination, or very rarely a late launch-evidence
anomaly attached to an already-terminalized request), or `crashed` (janitor-composed only, never
from a `stage:"exited"` result).

## Verifier (`verify_run.py`)

`plans/longcontext-pow2-overnight-campaign.md` WP-4. Post-queue mechanical verification over a
night's already-written records and the batch runner's already-persisted queue state — the seam
`batch verify --run <dir>` dispatches to (`batch.py cmd_verify`, unedited by this WP: it already
calls `verify_run.main(["--run", <dir>])` and expects an int exit code). **No interpretation
authority**: this module runs a fixed, named checklist and reports what it found; see
`.claude/agents/verifier-bakeoff.md` below for the worker-tier agent built on top of it, which
carries the same restriction.

### Run-directory + queue-state conventions (this WP's own design call)

WP-2's live battery hookup (not yet landed at WP-4 write time) is what will actually populate a
directory shaped like this — WP-4 only defines, documents, and tests the shape it consumes:

- `--run <dir>` — a directory whose immediate subdirectories are one-per-queue-cell, each the
  standard four-file `run_harness.write_experiment_record` shape (`config.json`/`prompts.jsonl`/
  `completions.jsonl`/`samples.jsonl`), subdirectory name == that cell's `cell_id`
  (`experiment_id == cell_id` for every cell a batch queue runs).
- `--state <path>` — optional explicit queue state-file path; when omitted, tried in order:
  `<run-dir>/queue.state.json`, then `<run-dir's parent>/<run-dir's name>.state.json`
  (sibling-of-run-dir, same shape `batch.default_state_path` uses for a queue file, applied here
  to the run directory's own path). No match — state-derived checks degrade to `unknown`, never
  silently skipped from the report.

### Checklist

Every item is one `{check, cell_id, expected, observed, verdict}` unit, `verdict` in
`("pass", "fail", "unknown", "informational")`:

- `records_present` — a records subdirectory exists for this cell; **required** (`fail` if
  absent) for a `completed` cell, optional (`unknown` if absent) for any other state.
- `schema_conformant` — `config.json` carries every frozen v2 top-level key (`schema_version`,
  `experiment_id`, `timestamp`, `lineage`, `sweep_cell`, `wilson_summary`, `size_check`) plus
  the full `run_harness.REQUIRED_LINEAGE_FIELDS` set, and every parsed `samples.jsonl` row
  carries its type's required v2 fields (the full `longcontext_probe` field list transcribed
  from `longcontext.run_cell`'s own sample dict; the `sampling_arm`/`temperature`/`top_p`/
  `top_k`/`sampler_seed`/`classifier_verdict` v2 gains for `needle_probe`/
  `quality_corpus_result`; `timing_round` only needs a `type` key — it never gained v2 fields).
- `vram_arithmetic` — consumes `sweep.kv_bytes_per_token` (never reimplements the KV-bytes
  formula) against the cell's declared KV arm (`-ctk`/`-ctv`/`-c` from the recorded
  `sweep_cell.server_flags`) and geometry resolved from a recorded `sweep_cell.
  synthetic_geometry` fixture block or a still-present `model_host_path` GGUF header (the same
  live-header path `sweep.classify_fit` uses); compares `weights_bytes + kv_bytes_at_context`
  against the recorded `vram.loaded_bytes - vram.baseline_bytes` delta within a generous
  `[0.4x, 3.0x]` multiplicative band (`VRAM_ARITHMETIC_LOW_RATIO`/`_HIGH_RATIO`) — wide enough to
  absorb `kv_bytes_per_token`'s own documented ~1.7x live MLA approximation error without
  false-positiving, narrow enough to still catch a KV-type-scale-class mismatch (the "silent
  wrong region" shape the campaign hunts, e.g. a quantized-KV cell whose measured delta actually
  matches its f16 sibling's). `unknown` (never `fail`) whenever geometry can't be resolved.
- `prompt_tokens_tolerance` — every `longcontext_probe`/`needle_probe` sample's recorded
  `prompt_tokens` within ±10% of `target_tokens` (plan Test Plan's pinned tolerance).
- `wilson_min_trials` — whenever `config.json`'s `wilson_summary` is non-null, its `n` is
  `>= longcontext.MIN_TRIALS_FOR_WILSON` (consumed, never re-pinned as a second literal `10`).
- `classifier_distribution` — always `informational`: a per-cell count of `classifier_verdict`
  values — reported, never gated on, matching this README's own "never silently trusted... a
  manual spot-check is expected practice" framing for classifier output (see the Degeneracy
  classifier section below).
- `abort_state` — one queue-level (`cell_id: null`) item: `fail` iff the queue state's
  `queue_state` is `batch.QUEUE_ABORTED_HANG`, `unknown` iff no state file was found, else
  `pass`; `observed` always lists every cell in `batch.ABORTED_HANG` state too, so an
  `aborted-hang` queue is unmissable in both reports even before a reader reaches the per-cell
  detail (both report writers put this check first).

### Reports + exit codes

Two reports are written into `--run <dir>`: `verify_report.json` (the full checklist item list
plus a `summary` count) and `verify_report.md` (the same information for a human, with an
`## ⚠ QUEUE ABORTED-HANG ⚠` banner first when applicable). Exit code: `0` — every item
`pass`/`unknown`/`informational`; `1` — any item `fail` (this includes an `aborted-hang` queue,
via `abort_state`); `2` — the run directory doesn't exist, or exists but yields nothing to check
at all (no discoverable queue state file and no per-cell subdirectories) — "cannot verify",
never silently reported as a clean `0`. Neither report is written when the run directory is
entirely absent (nowhere to write them).

### CLI

```sh
python3 llama/bakeoff/verify_run.py --run <run-dir> [--state <state.json>]
python3 llama/bakeoff/batch.py verify --run <run-dir>            # same seam, via batch.py
```

### Worker-tier agent profile (`.claude/agents/verifier-bakeoff.md`)

Plan Decisions locked: "Verification is mechanical: automated checks in the runner + a
worker-tier Claude agent profile... with mechanical instructions for post-run triage." The
installed profile runs `batch verify --run <dir>`, reads both reports, and files a summary of
the machine-readable verdicts verbatim — flagging any `fail`/`aborted-hang` prominently — with
explicit instructions never to reinterpret, soften, or override a check result.

### Local-inference seam (designed, not built)

Plan Decisions locked: "the design must leave a clean seam for replacing that agent with a
local-inference verifier... once the quality bar is validated." Every checklist item already IS
the self-contained `{check, cell_id, expected, observed, verdict}` unit a future local-model call
would answer — none of the five keys are Claude-shaped prose. A future local-inference verifier
(the GLiNER2-extract / bge-m3-similarity precedent from the Ling plan — a small always-resident
CPU-tier model per `onnx/`, not a frontier agent) would consume exactly this module's JSON report
and answer one narrow question per item ("does `observed` satisfy `expected`?") without ever
touching the underlying records itself. That successor is out of scope here — `verify_run.py`
only produces the report shape it would consume.

## Long-context correctness instrument v2 (`longcontext.py`)

`plans/quant-sweep-harness-and-instrument-v2.md` WP-3, built on WP-2's research
(`docs/long-context-collapse.md`). **Why this exists as a separate module from `run_harness.
needle_probe`, not a mode of it**: `needle_probe`'s haystack (`_build_filler_text`) is uniform
random words from a fixed bank — explicitly a *timing* fixture, never quality-scored (Decisions
locked). A degenerate completion after that input cannot be attributed to genuine long-context
collapse versus out-of-distribution-input mirroring; this repo's own captured evidence (two
degenerate `gemma-4-12b-qat` fragments, `llama/bakeoff/tests/fixtures/degenerate_fragments.json`)
shows the degenerate word-salad vocabulary matching that exact word bank. `_build_filler_text`
stays exactly as it was; `longcontext.py` is the correctness-scored replacement, never the other
way around.

### Corpus (`corpus/`)

12 public-domain Project Gutenberg books across 6 distinct domains, plain text, Gutenberg
header/footer boilerplate stripped, each prefixed with a one-line provenance note (also works
around a libmagic false-positive in some environments where a file's first three bytes being
literally `MED` gets misidentified as an Amiga tracker module — verified live during the
original 5-book WP; applied uniformly to all twelve books rather than special-casing the one
that needed it):

| id | title | author | domain | corpus |
| --- | --- | --- | --- | --- |
| `pride-and-prejudice` | Pride and Prejudice | Jane Austen | fiction | v1+v2 |
| `origin-of-species` | On the Origin of Species... | Charles Darwin | natural science | v1+v2 |
| `franklin-autobiography` | The Autobiography of Benjamin Franklin | Benjamin Franklin | history / memoir | v1+v2 |
| `meditations` | Meditations | Marcus Aurelius | philosophy | v1+v2 |
| `the-prince` | The Prince | Niccolo Machiavelli | political philosophy | v1+v2 |
| `moby-dick` | Moby Dick; Or, The Whale | Herman Melville | fiction | v2 only |
| `the-republic` | The Republic | Plato (trans. Benjamin Jowett) | philosophy | v2 only |
| `a-tale-of-two-cities` | A Tale of Two Cities | Charles Dickens | fiction | v2 only |
| `walden` | Walden, and On The Duty Of Civil Disobedience | Henry David Thoreau | nature / philosophy | v2 only |
| `frankenstein` | Frankenstein; Or, The Modern Prometheus | Mary Wollstonecraft Shelley | fiction | v2 only |
| `huckleberry-finn` | Adventures of Huckleberry Finn | Mark Twain | fiction | v2 only |
| `sherlock-holmes` | The Adventures of Sherlock Holmes | Arthur Conan Doyle | fiction | v2 only |

Two manifests, two `corpus_id`s (`plans/longcontext-pow2-overnight-campaign.md` WP-3 —
append-only extension, never a rename of v1): **v1** (`gutenberg-longcontext-haystack-v1`,
`corpus/manifest-v1.json`, the 5 `v1+v2`-marked books above, ~2.72M chars) is frozen
byte-for-byte so records produced under it stay reproducible, and is never the default manifest
again. **v2** (`gutenberg-longcontext-haystack-v2`, `corpus/manifest.json`) is now
`load_corpus_manifest()`'s default (and the battery's) — all 12 books above, ~8.08M chars, total
corpus directory ≈7.8 MiB (`du -sb llama/bakeoff/corpus/`), inside the plan's 8 MiB cap
(`tests/test_corpus.py` asserts this against the default manifest's book list). v2 exists
because a 512k-token-equivalent haystack against v1 consumed ~85% of the corpus, collapsing
per-book start offsets to near-zero-variance across seeds; against v2 the same target consumes
<35% (audit M1 — see "Haystack assembly" below).

`corpus/manifest.json`: `corpus_id` (this is the value every emitted sample's
`corpus_manifest_id` field carries) plus, per book, `{id, title, author, domain, source_url,
gutenberg_id, file, sha256, license_note, char_count, file_size_bytes}` (the 7 books added for
v2 also carry `fetched`, their fetch date). All twelve books are public domain (pre-1929 US
publication); the distributed Gutenberg text itself carries the Project Gutenberg License
(gutenberg.org/policy/license.html) — `license_note` restates this per book.

**Refresh note**: `sha256`/`char_count` are computed AFTER stripping — recomputing them against
the committed files must reproduce `manifest.json` exactly (`tests/test_corpus.py` asserts
this). If a book is ever re-fetched (Gutenberg text can be revised), re-run the same
strip-and-hash procedure (locate `*** START/END OF THE PROJECT GUTENBERG EBOOK ... ***`, keep
only the text between them, prepend the one-line provenance note) and update `manifest.json`'s
`sha256`/`char_count`/`file_size_bytes` together — a stale hash here is a silent corpus-drift
bug, not caught by anything except this test.

### Haystack assembly

`assemble_haystack(rng, target_tokens, manifest=None, chars_per_token=None)` — `rng` (a
`random.Random(haystack_seed)` instance) drives ALL haystack randomness (book shuffle order,
per-book passage start offsets); `sampler_seed` never does (separate-seed-fields contract,
inherited from `run_harness.needle_probe`). Cycles through the corpus books, taking one
contiguous, randomly-positioned passage per book per pass, concatenated with `"\n\n"`, until the
assembled text reaches `target_tokens * chars_per_token` (default
`run_harness.CHARS_PER_TOKEN_ESTIMATE`) characters — a sizing TARGET only; the real on-wire
token count always comes back from the server's own `usage.prompt_tokens`, exactly like
`_build_filler_text`.

**Documented band: realistic sweep targets up to and including the 512k-token-equivalent
(524288-token) regime, tested directly against corpus v2** (`tests/test_haystack.py`'s
`TestHaystack512kRegime`, against the real on-disk corpus — no synthetic stand-in). Whenever a
single book can satisfy the whole remaining need on its own, it does: one book, one randomized
offset, one pass — still the common case at small-to-mid targets against v2, and still available
at 512k depending on shuffle order and target size. When no single book can (multi-book
concatenation), each pass's bite is capped to a FAIR SHARE of what's left
(`ceil(remaining_needed / books_left_this_lap)`, recomputed every pass over the books not yet
visited in the current lap) rather than greedily draining whichever book comes first in the
shuffle order — the audit-M1 fix for the pre-v2 whole-book, zero-offset re-emission bug. The old
greedy strategy front-loaded entire books (offset forced to 0 -- no unused content left to vary)
into however many books it took to reach the target, so at the 512k target against the old
~2.72M-char v1 corpus (a haystack there consumed ~85% of it), all but the walk's last book
degenerated to identical whole-book content on every seed, and cross-seed `haystack_sha256`
differences came entirely from book-order shuffling, never real passage-content variation
(seeded book-order shuffling alone already made the digest differ across seeds on the unmodified
code — a digest-inequality test alone passes vacuously). With the fair-share cap, a book still
degrades gracefully to a deterministic whole-book, offset-0 take exactly when it's shorter than
its fair share (still no unused content to vary); every book long enough to exceed its slice
keeps a genuinely `rng`-drawn start offset. Multi-book concatenation is exercised both by
`tests/test_haystack.py`'s synthetic-corpus unit tests (tight boundary cases) and by the real
corpus v2 at the 512k regime.

`insert_at_fraction`/`insert_multiple` snap injection points to the next sentence boundary
(matching `needle_probe`'s existing `". "`-boundary convention). `insert_multiple` requires
`fractions` ascending and processes insertions in descending-index order — this correctly
resolves the case where two fractions snap to the identical boundary (a real tie, observed live
against this corpus at the multi-needle task's 0.03 spread), which a naive stable
sort-by-boundary-value would silently invert; see that function's docstring and
`tests/test_haystack.py`'s regression test.

### Task battery

Three tasks (`longcontext.TASKS`), each scored independently, never merged:

- **`single_needle`** — literal-match recall (NIAH-style): a unique reference code injected at
  `position_fraction`, the model must reproduce it verbatim.
- **`multi_needle`** — 3 distinct reference codes injected in ascending order, clustered near
  `position_fraction` (0.03-fraction spread) rather than at 3 independent positions —
  `position_fraction` stays the single per-cell independent variable; correct requires all 3
  codes present AND recovered in original order (ordered readback).
- **`passage_qa`** — passage-anchored QA, the NON-literal-recall half of doc Req 1's "separate
  literal-match recall from non-literal recall as distinct battery tasks": a short synthetic
  passage (`PASSAGE_QA_ITEMS`, 4 items) states a fact using an invented, distinctive multi-word
  answer term; the paired question asks about it via paraphrase and NEVER repeats the answer
  term verbatim (`tests/test_tasks.py` asserts this programmatically for every item) — the model
  must locate the right passage semantically, not by grepping the question against the haystack
  (NoLiMa-class). A pass on `single_needle` does not bound performance here.

### Position sweep + statistics

`POSITION_FRACTIONS = (0.10, 0.45, 0.85)` — the plan's first-class position cells.
`run_cell(request_fn, task, cell_id, position_fraction, target_tokens, sampling_arm,
sampling_payload, n_trials=10, ...)` runs one (task, position) cell for `n_trials` distinct
`haystack_seed`s (deterministic schedule below), emitting one schema-v2 `longcontext_probe`
sample per trial plus (only at `n_trials >= 10`) a per-cell Wilson-interval failure-rate
summary. `run_position_sweep(...)` runs one task across all 3 positions as INDEPENDENT cells,
returned in a dict keyed by `position_fraction` — **never averaged or merged across positions or
tasks** (doc Req 5 / plan Decisions locked); a caller wanting one rollup number is doing
something this module deliberately does not do.

**No live Docker/GPU execution lives here.** `request_fn(prompt, max_tokens, sampling) ->
result_dict` is the caller's own live-request hook (matching `run_harness.
_execute_streaming_request`'s return shape) — a live orchestrator wires it to the real router;
`longcontext.py`'s own tests pass a deterministic offline stub and never touch the network.

**Sampling is pinned, mechanically enforced** (doc Req 3): `run_cell` raises `ValueError` if
`sampling_arm` isn't `"capability"`/`"production"`, or if `sampling_payload` is missing/has any
of `temperature`/`top_p`/`top_k`/`seed` as `None` — build it with
`run_harness.sampling_arm_payload(SAMPLING_ARM_CAPABILITY, seed)`, or fill in the specific
model's recommended values for the production arm first. `sampling_payload["seed"]` is
overridden per-trial by this driver's own `sampler_seed` schedule regardless of what's passed in
(every trial gets a distinct sampler seed, never one shared across a cell's `n_trials`).

**Deterministic per-trial seed schedule**: `derive_trial_seed(cell_id, trial_index, purpose)` —
`sha256(f"{cell_id}:{trial_index}:{purpose}")` truncated to a positive 31-bit int, `purpose` ∈
`{"haystack", "sampler"}`. `haystack_seed` and `sampler_seed` are always distinct fields on every
sample, never one shared derivation — re-running the same cell reproduces the identical trial
sequence.

**Wilson intervals**: `wilson_interval(k, n, sided, confidence=0.95)` — `sided` (`"one"`/`"two"`)
is a REQUIRED parameter with no default (the plan's binding "state the convention explicitly at
every use site" pin, correcting the cycle-2 audit's exact one-sided/two-sided mixing finding).
`"one"` returns only an upper bound (`lower` is `None`) — the convention for a directional "is
the failure rate below X%" claim (e.g. the plan's own cited "0/30 Wilson upper = 8.27%",
reproduced by `tests/test_statistics.py`). `"two"` returns both bounds. Only 95% two-sided/
one-sided are tabulated (`_Z_TABLE`) — any other confidence raises `ValueError` rather than
guessing a z-value. A cell's failure-rate summary counts every non-`"correct"` verdict as a
failure (wrong + degenerate + truncated(benign) all count) — this module's own call, restated on
the summary dict itself (`sided`/`confidence` keys always present) so nothing downstream has to
guess the convention from context.

`haystack_structure` (doc Req 2) is recorded on every sample, `"coherent"` by default
(`HAYSTACK_STRUCTURE_COHERENT`) — this module's only implemented assembly mode; a documented
variable field, not an assumed-safe constant, so a future shuffled-haystack arm (testing
Chroma's measured shuffle-vs-flow effect, `docs/long-context-collapse.md` §1) is a parameter
value, not a rewrite.

### Degeneracy classifier

`classify_trial(reasoning_text, content_text, finish_reason, correct=None, haystack_text=None)`
→ `ClassifierResult(verdict, shape, flags)`. Verdict ∈ `VERDICTS = ("correct", "wrong",
"degenerate", "truncated(benign)")`; when `degenerate`, `shape` ∈ `DEGENERATE_SHAPES =
("incoherent", "repetition", "special_token_spam")` (doc Req 6: tag degeneration SHAPE, not just
pass/fail — each shape implicates a different cause). `compute_flags(...)` computes, over the
full `reasoning_text` + `content_text` stream: `max_ngram_repetition_rate`, `distinct_2_ratio`,
`haystack_vocabulary_overlap` (only when `haystack_text` is supplied — informational, confirms
input-mirroring when the generating haystack is known; not gating on its own),
`special_token_match_count`, `core_function_word_count`, `content_channel_never_reached`,
`budget_exhausted_in_reasoning`.

**Checked in priority order, thresholds and rationale** (this WP's discretion, per the plan's
classifier spec — chosen and verified against `tests/test_classifier.py`'s fixtures, not merely
asserted):

1. **`special_token_spam`** — `special_token_match_count >= 3`
   (`<unused\d+>`-class / `<|...|>`-style markers, doc §6 / upstream issue #21321). A count
   floor, not a single-mention trigger, so one incidental literal mention (e.g. a
   tokenizer-internals discussion) never misfires.
2. **`repetition`** — `token_count >= 20` AND (`max_ngram_repetition_rate >= 0.20` (4-grams) OR
   `distinct_2_ratio <= 0.40`) — Holtzman-style entropy-collapse/loop shape.
3. **`incoherent`** — `token_count >= 15` AND ZERO occurrences of any of a small "core"
   function-word set (`the a an of and or in on to for with is are this that`) in the stream.
   **This is the classifier's most load-bearing, and most carefully measured, threshold**: the
   two real captured degenerate fragments (`tests/fixtures/degenerate_fragments.json` — verbatim
   wire captures, `runs/dispatch/qshiv2-wp-3-fixture-source.md`) both score EXACTLY 0 core
   function words (68 and 40 tokens respectively — pure jargon-noun concatenation, no grammar).
   The 23 committed clean `gemma-4-12b-qat` rows (`llama/bakeoff/records/gemma-4-12b-qat-full/
   completions.jsonl`) — including several `timing_round` rows sharing the fragments' exact
   metadata shape (`finish_reason: "length"`, `content_text` empty, since they're the old
   word-soup fixture cut off by `max_tokens` mid-list) — score a MINIMUM of 3 core function
   words. A margin of 0 vs. 3+, not a fractional-ratio threshold near a boundary, is what makes
   this reliable rather than fragile; see `tests/test_classifier.py`'s
   `TestCleanGemmaQatRowsClassifyNonDegenerate` and `TestDegenerateFragmentsAreGroundTruthPositives`.

**Important, empirically load-bearing**: `content_channel_never_reached` +
`budget_exhausted_in_reasoning` (`finish_reason == "length"` with an empty content channel) is
**NOT, by itself, sufficient evidence of `degenerate`** — those same clean `timing_round` rows
above exhibit exactly that combination while reasoning in genuinely fluent, on-topic prose that
simply ran out of budget. That combination alone classifies `truncated(benign)`, not
`degenerate`; only the three content-shape checks above (1–3) classify `degenerate`. A caller
that assumed "empty content + finish_reason length = degenerate" without this module would have
misclassified 3 of this repo's own 23 clean fixture rows.

Verdicts are recorded per-trial (`classifier_verdict`/`classifier_shape`/`classifier_flags` on
every `longcontext_probe` sample), **never silently trusted** — a manual spot-check of a sample
of `degenerate`/`truncated(benign)` verdicts against their `completions.jsonl` text is expected
practice before citing a rate in a results doc, same as the existing quality-corpus manual-
scoring convention above.

### Live usage sketch (not executed by this module or its tests)

```python
from llama.bakeoff import run_harness, longcontext

def request_fn(prompt, max_tokens, sampling):
    return run_harness._execute_streaming_request(
        base_url, alias, [{"role": "user", "content": prompt}], max_tokens,
        temperature=sampling["temperature"], top_p=sampling["top_p"],
        top_k=sampling["top_k"], seed=sampling["seed"],
    )

payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=20260823)
results = longcontext.run_position_sweep(
    request_fn, longcontext.TASKS["single_needle"], "gemma-4-12b-qat-c65536",
    target_tokens=31000, sampling_arm="capability", sampling_payload=payload,
)
# results[0.10].summary, results[0.45].summary, results[0.85].summary -- reported PER CELL,
# never averaged (doc Req 4/7/8/9 -- FA/KV-type verification, llama.cpp-build-fix verification,
# -b/-ub fixed, context-shift-off verification -- are live-run/campaign concerns the deferred
# `gemma-qat-longcontext-campaign` plan owns; this instrument assumes the caller already
# verified them per llama/AGENTS.md's standing invariants, it does not re-verify them itself).
```

Offline sanity check (touches no Docker/network/GPU, matching `sweep.py --dry-run`):

```sh
python3 llama/bakeoff/longcontext.py --dry-run --target-tokens 31000
```

## Schema v2 (FROZEN 2026-08-23)

`plans/quant-sweep-harness-and-instrument-v2.md` WP-1 made the producer (`write_experiment_
record`, `needle_probe`, `run_quality_corpus`) v2-conformant from day one; **WP-4 froze this
field list as the versioned DB contract** the sibling `bench-results-db-and-dashboard` plan
ingests. The freeze covers every field bullet below, including WP-3's `longcontext_probe`
sample type. Conformance was verified at freeze time over every record this plan's WPs
produced: `llama/bakeoff/records/wp1-smoke-cell-ling-q4km/` (config lineage keys, `sweep_cell`/
`wilson_summary` presence, per-sample v2 fields, all four files parse clean) and a
stub-driven `longcontext.run_cell` sample (all fields below present, seed fields distinct,
JSON-serializable). Pre-existing v1 records on disk remain as-is — the DB plan ingests v2
only (pinned there), with one narrow, allowlisted exception (`plans/bakeoff-timing-records-and-estimates.md` WP-4): a pre-v2 record dir whose `samples.jsonl` contains `timing_round` rows gets those rows — and ONLY those — ingested under explicit `schema_origin: "v1-timing"` provenance (selection is content-based, never a name glob); every other kind of legacy content stays un-ingested. **Changing or removing a frozen field is a schema-v3 event** (bump
`schema_version`, document the migration here), never a silent edit; purely additive fields
may append to v2 with a dated note in this section.

- `config.json` gains `schema_version: 2` (unconditional — this producer no longer emits v1;
  pre-existing v1 records on disk are untouched), `sweep_cell` (the sweep-cell identity a
  sweep-driven caller supplies; `null` for a non-sweep `full` CLI record), and `wilson_summary`
  (**deferred audit pin**: always present as a key, `null` whenever the record has no replicated
  correctness cells (n>=10) to summarize with a Wilson interval — never omitted).
- Every `needle_probe`/`run_quality_corpus` sample in `samples.jsonl` gains: `sampling_arm`
  (`"capability"`/`"production"`/`null`, a provenance label), `temperature`/`top_p`/`top_k`
  (whatever was actually sent, `null` if omitted), `sampler_seed`, and `classifier_verdict`
  (**nullable now — the classifier arrives in WP-3**).
- `needle_probe` samples additionally gain, per the **separate-seed-fields deferred audit pin**:
  `haystack_seed` (the haystack-generation seed, distinct from `sampler_seed` — never one shared
  value even when a caller passes the same integer for both), `corpus_manifest_id` (`null` here
  — this instrument still uses the word-soup timing-fixture haystack, not WP-3's real corpus,
  which is the only haystack source with a manifest), and `haystack_sha256` (sha256 of the
  assembled prompt text — the haystack-by-reference field; the full haystack text itself is
  never persisted, matching the pre-existing `seed_used`/`target_tokens`/`position_fraction`
  reproducibility contract).
- **WP-3 adds a new sample `type`, `"longcontext_probe"`** (`longcontext.run_cell`), carrying
  every field above PLUS `task`/`cell_id`/`trial_index`/`haystack_structure`/`haystack_book_ids`/
  `classifier_shape`/`classifier_flags`/`trial_meta` — `corpus_manifest_id` is non-`null` here
  (`"gutenberg-longcontext-haystack-v2"` since WP-3 made the v2 manifest the default; v1 stays
  on disk for reproducing pre-WP-3 records), the inverse of `needle_probe`'s always-`null`
  value, since this IS the corpus-backed instrument. See "Long-context correctness instrument v2" above
  for the full field-by-field description; this sample type is part of the frozen v2 contract.

#### Additive v2 fields — 2026-08-25 (`plans/bakeoff-timing-records-and-estimates.md` WP-1)

Purely additive, per the freeze rule directly above: **`schema_version` stays 2**, no existing
field is renamed, repurposed, or removed, and every field below is optional and nullable. A
record written before this date simply lacks these keys, and every consumer (dashboard ingest,
`verify_run.py`, the queue estimator) must keep parsing such a record unchanged — that
back-compat is asserted in the stdlib suite, not just asserted here.

- **`config.json`, four new top-level keys** — the per-cell clock the model/context-aware queue
  estimator needs:
  - `cell_started_at` / `cell_ended_at` — ISO-8601 UTC. The start is taken at the TOP of the
    cell's scratch-server context (before the pre-spawn orphan sweep), so the interval covers the
    cell's whole occupancy of the GPU, not just its battery.
  - `cell_wall_seconds` — float, measured from `time.monotonic()`, **never** by differencing the
    two ISO strings (a wall-clock step must not be able to contaminate a duration).
  - `scratch_startup_seconds` — float or `null`: the scratch server's spawn → startup-verification
    elapsed time (`sweep._ScratchServerContext.__enter__`, previously an untimed wait). `null`
    whenever the server failed health or startup verification — a health timeout's elapsed time
    is the timeout, not a startup measurement. **This is deliberately NOT called
    `model_load_seconds`**: that name is reserved by the honest-timing-semantics contract at the
    top of this file for a `ResidencyPoller`-*observed* residency transition, and these are
    different measurements of different things. Never conflate or substitute them.
- **`config.json` `sweep_cell` gains `model`** — the queue model KEY (`cells[].model` in the
  queue file), the record-side model-identity pin the queue estimator groups its cost table by.
  `null` for a record whose cell dict carried no model key; never inferred from the alias
  (aliases and model keys are separate, independently-append-only vocabularies).
- **`longcontext_probe` samples gain three nullable fields** — `completion_tokens` (projected
  from the `usage` object that was already being parsed and partially projected), plus
  `server_prompt_eval_tok_s` and `server_eval_tok_s`, read only from llama.cpp's own `timings`
  response block (`prompt_per_second` / `predicted_per_second`; live-verified present on this
  build 2026-08-25). All three are `null` for any response that omits the source. **A decode rate
  is never derived from `wall_seconds` here** — a needle probe's wall time is prefill-dominated,
  so tokens ÷ wall would answer a different question while looking like throughput.
- **`timing_round` samples can now appear in a v2 record.** They are the SAME shape
  `run_harness.run_round` has always emitted (the pre-v2 field set), because the `"timing"`
  battery item reuses `run_round` verbatim rather than reimplementing it — see "The `timing`
  battery item" under the batch runner below.

### Sampling-arm presets

```python
from llama.bakeoff.run_harness import (
    SAMPLING_ARM_CAPABILITY, SAMPLING_ARM_PRODUCTION, sampling_arm_payload,
)

# capability: temperature 0, seeded -- deterministic-decoding shape, fixed numeric values.
payload = sampling_arm_payload(SAMPLING_ARM_CAPABILITY, seed=20260823)
needle_probe(base_url, alias, "moderate",
             temperature=payload["temperature"], top_p=payload["top_p"],
             top_k=payload["top_k"], sampler_seed=payload["seed"],
             sampling_arm="capability")

# production: the model's OWN recommended sampling, seeded -- SAMPLING_ARM_PRODUCTION carries
# None placeholders (this harness has no per-model sampling-recommendation table); the caller
# fills temperature/top_p/top_k in with the specific model's recommended values before use.
```

`temperature`/`top_p`/`top_k`/`seed` are omitted from the wire payload entirely when `None` — the
byte-identical-behavior guarantee for every pre-existing caller that doesn't pass them.

## Field-to-file split (this WP's own call — the plan pins only the four-file split itself)

- `config.json` — run-level metadata + the artifact-lineage field set (`hf_repo`, `hf_revision`,
  `filename`, `file_size`, `sha256`, `llama_cpp_build` — all six keys required, though a value
  may be `null` if genuinely unresolvable) + the `timing_summary` aggregate + `size_check`.
- `batch.py`'s `write_cell_record` (queue-driven `matrix-*`/`ladder-*` records) derives `hf_repo` as `"<org>/<repo>"` from the cell's `model_host_path` when it matches `/models/<org>/<repo>/<file>` or the older `/srv/models/<org>/<repo>/<file>` (else `null`) — both prefixes are accepted permanently, since `model_host_path` is baked into the frozen `cell_id` identity hash for every queue authored before the `/models` partition migration, and rewriting those host paths to the new prefix would fork their `cell_id`s — and sets `llama_cpp_build` from `run_harness.resolve_llama_cpp_build()` (the pinned `llama/Dockerfile` tag) — both always attempted, `llama_cpp_build` never `null`. `hf_revision`/`sha256` stay explicit `null` on every queue-driven record; neither has a cheap derivation. Records written before this fill rule existed are never rewritten — their `hf_repo`/`llama_cpp_build` stay `null` forever, and a consumer reading historical records recovers the repo from `sweep_cell.model_host_path` itself.
- `prompts.jsonl` — the quality-corpus prompt text only (one line per corpus entry, copied
  verbatim from `quality_corpus.jsonl` at write time so the record is self-contained).
- `completions.jsonl` — every raw completion verbatim: timing-sweep rounds, needle-probe answers,
  quality-corpus answers. Never truncated, summarized, or lossy-transformed.
- `samples.jsonl` — numeric timing/sample statistics per round (`type: "timing_round"`), needle
  probe pass/fail + generation parameters (`type: "needle_probe"`), and quality-corpus per-answer
  records including the manual-scoring placeholders (`type: "quality_corpus_result"`).

## Gate reference

- `python3 -m py_compile llama/bakeoff/run_harness.py llama/bakeoff/sweep.py
  llama/bakeoff/longcontext.py` — must exit 0 (root `AGENTS.md` Gates #1).
- `python3 llama/bakeoff/sweep.py --dry-run llama/bakeoff/examples/sweep-example.json` — expands
  the 3-cell fixture, prints per-cell fit classification and the exact command each cell would
  run; non-empty, deterministic, touches no Docker.
- `python3 -m unittest discover -s llama/bakeoff/tests -p 'test_*.py' -v` — the
  `longcontext.py` classifier/haystack/task/statistics/cell-driver suite; green, with a nonzero
  discovered-test count (no package `__init__.py` under `llama/` — bare root discovery can
  green-pass zero tests here; each `tests/test_*.py` file inserts `llama/bakeoff` onto
  `sys.path` itself so `import longcontext`/`import run_harness` resolve regardless of
  discovery's own top-level-dir choice). Touches no Docker/network/GPU.
- `du -sb llama/bakeoff/corpus/` — must be ≤ 8388608 bytes (8 MiB, plan-pinned cap).
- Corpus manifest integrity: recompute every book's sha256 against `corpus/manifest.json` and
  diff — `tests/test_corpus.py` runs this automatically as part of the suite above; it can also
  be run standalone (see that test file).
- `python3 llama/bakeoff/longcontext.py --dry-run --target-tokens 31000` — assembles one trial
  per task/position from the real committed corpus, prints JSON; non-empty, deterministic,
  touches no Docker/network/GPU.
