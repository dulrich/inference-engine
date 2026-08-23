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

- `run_harness.py` — the harness itself (timing sweep, needle probe, quality corpus, record
  writer, CLI).
- `sweep.py` — the declarative sweep framework: spec loading/expansion, the VRAM-fit pre-filter,
  ladder expansion, deterministic cell IDs, exact-command building, and the scratch-server
  lifecycle (spawn/health-poll/verify/teardown/VRAM-read-back). See "Sweep framework" below.
- `longcontext.py` — the long-context correctness instrument v2: coherent haystack corpus
  loading/assembly, the 3-task battery, the position sweep + n-trial cell driver, Wilson-interval
  statistics, and the degeneracy classifier. See "Long-context correctness instrument v2" below.
- `corpus/` — the coherent public-domain haystack corpus (Project Gutenberg) `longcontext.py`
  assembles trials from, plus `corpus/manifest.json`'s provenance record. See that section.
- `tests/` — stdlib `unittest` suite for `longcontext.py` (corpus integrity, haystack assembly,
  task battery, classifier — including the real captured degenerate fragments and the committed
  clean gemma-qat transcripts as fixtures — statistics, and the cell driver). Run with
  `python3 -m unittest discover -s llama/bakeoff/tests -p 'test_*.py' -v`.
- `examples/sweep-example.json` — a 3-cell fixture for `sweep.py --dry-run` (synthetic geometry,
  no real model file required — see that section).
- `quality_corpus.jsonl` — 12 fixed prompts across the 8 categories in
  `references/serving-harness-test-round-2.md` §5 (factual/technical QA, code/debug reasoning,
  multi-constraint instruction following, structured transformation, ambiguity/judgment,
  correction uptake, long-form synthesis, long-context retrieval), including 2 deterministic
  sentinels (`qa-2`, `longctx-1`) that do not dominate the set. Reused identically across every
  quant of a given model — do not parameterize it per-model.
- Raw experiment records land in `runs/bakeoff/<experiment-id>/` (tracked and committed — see
  "Honest-timing-semantics contract" below and the plan's Decisions locked section for why this
  reverses the more common "runs/ is scratch" convention).

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
  reviewer should revert any change that does this.
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
  --gguf-host-path /srv/models/unsloth/.../Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
  --context-ceiling 131072
```

Runs the sweep, both needle probes (moderate always; near-ceiling only if `--context-ceiling` is
given), and the quality corpus, then writes
`runs/bakeoff/<experiment-id>/{config.json,samples.jsonl,prompts.jsonl,completions.jsonl}`.

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
      "model_host_path": "/srv/models/org/repo/file-Q4_K_M.gguf",
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
- `battery` — subset of `{"vram", "timing", "needle", "quality"}`.
- `cell_id` (optional) — explicit override for the deterministic per-cell ID; omit to have
  `compute_cell_id()` derive one from the cell's identity-bearing fields (never random, never
  timestamped — the same spec always reproduces the same IDs).

### VRAM-fit pre-filter

`classify_fit(cell)` returns one of `"fits"` / `"marginal"` / `"excluded"` / `"unknown"` (the
last when neither `model_host_path` nor `synthetic_geometry` is usable — never a guessed fit),
plus the numbers behind the classification. Formula:
`estimated_total = weights_bytes + kv_bytes_per_token(ctk, ctv) * -c + NON_WEIGHT_OVERHEAD_GIB`,
classified against `REALISTIC_FREE_VRAM_GIB` with a `FIT_MARGIN_GIB`/`MARGINAL_MARGIN_GIB` band.
Every constant is cited in `sweep.py`'s own comments to a specific prior bake-off doc line or
this plan's own text — see the module for the full citations (card nameplate 15.92 GiB; the
plan's own "~12.8 GiB realistic free VRAM" sizing budget, `plans/quant-sweep-harness-and-
instrument-v2.md:50-51`; ~456 MiB non-weight overhead, `docs/qwen38-27b-bakeoff-results.md:177-
179`; the "~1.5-2 GiB safety-margin convention" repeated across every prior bake-off doc).
**This is a pure-math predictor, never a substitute for measurement** — the framework's house
rule (plan Key Changes/WP-1): estimates gate what gets attempted, a live VRAM read-back
(`read_vram_used_bytes()`) is still what a real run trusts.

KV-cache byte-per-element table (`KV_TYPE_BYTES_PER_ELEMENT`) covers `f32`/`f16`/`bf16` (exact)
and `q8_0`/`q8_1`/`q5_0`/`q5_1`/`q4_0`/`q4_1` (ggml's own 32-element block-quant layout,
`block_bytes / 32`) — cross-checked against this repo's own live measurement in
`docs/qwen38-27b-bakeoff-results.md:158` (the q4_0 vs. q8_0 "KB/cell" comparison).

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

5 public-domain Project Gutenberg books across 5 distinct domains, plain text, Gutenberg
header/footer boilerplate stripped, each prefixed with a one-line provenance note (also works
around a libmagic false-positive in some environments where a file's first three bytes being
literally `MED` gets misidentified as an Amiga tracker module — verified live during this WP;
applied uniformly to all five books rather than special-casing the one that needed it):

| id | title | author | domain |
| --- | --- | --- | --- |
| `pride-and-prejudice` | Pride and Prejudice | Jane Austen | fiction |
| `origin-of-species` | On the Origin of Species... | Charles Darwin | natural science |
| `franklin-autobiography` | The Autobiography of Benjamin Franklin | Benjamin Franklin | history / memoir |
| `meditations` | Meditations | Marcus Aurelius | philosophy |
| `the-prince` | The Prince | Niccolo Machiavelli | political philosophy |

`corpus/manifest.json`: `corpus_id` (`"gutenberg-longcontext-haystack-v1"` — this is the value
every emitted sample's `corpus_manifest_id` field carries) plus, per book, `{id, title, author,
domain, source_url, gutenberg_id, file, sha256, license_note, char_count, file_size_bytes}`. All
five books are public domain (pre-1929 US publication); the distributed Gutenberg text itself
carries the Project Gutenberg License (gutenberg.org/policy/license.html) — `license_note`
restates this per book. Total committed size ≈2.6 MiB, inside the plan's 8 MiB cap
(`du -sb llama/bakeoff/corpus/`).

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
`_build_filler_text`. For every realistic sweep target (up through the near-ceiling 0.85 *
65536-token band), a single corpus book covers it in one pass; multi-book concatenation is
implemented and unit-tested (`tests/test_haystack.py`) but only engages for a target larger than
the largest single book.

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
   The 23 committed clean `gemma-4-12b-qat` rows (`runs/bakeoff/gemma-4-12b-qat-full/
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
produced: `runs/bakeoff/wp1-smoke-cell-ling-q4km/` (config lineage keys, `sweep_cell`/
`wilson_summary` presence, per-sample v2 fields, all four files parse clean) and a
stub-driven `longcontext.run_cell` sample (all fields below present, seed fields distinct,
JSON-serializable). Pre-existing v1 records on disk remain as-is — the DB plan ingests v2
only (pinned there). **Changing or removing a frozen field is a schema-v3 event** (bump
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
  (`"gutenberg-longcontext-haystack-v1"`), the inverse of `needle_probe`'s always-`null` value,
  since this IS the corpus-backed instrument. See "Long-context correctness instrument v2" above
  for the full field-by-field description; this sample type is part of the frozen v2 contract.

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
