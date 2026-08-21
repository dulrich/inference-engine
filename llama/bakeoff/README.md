# llama/bakeoff — serving-harness bake-off tooling

Reusable measurement tooling for `plans/serving-harness-round-2.md`, built once in WP-1 and
called by every later WP (WP-2 through WP-5) against a different already-configured router
alias. There is no alias-specific code path anywhere in `run_harness.py` — the harness works
against any alias the router already knows how to spawn.

Zero new runtime dependencies: Python 3 stdlib only (`urllib`, `json`, `time`, `statistics`,
`hashlib`, `threading`, `argparse`, `random`, `uuid`). No `requests`, no venv, no
`requirements.txt` — a from-scratch worker can run this with nothing beyond what's already on
the host image.

## Files

- `run_harness.py` — the harness itself (timing sweep, needle probe, quality corpus, record
  writer, CLI).
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

`python3 -m py_compile llama/bakeoff/run_harness.py` — must exit 0 (root `AGENTS.md` Gates #1,
added in this WP for the repo's first Python files).
