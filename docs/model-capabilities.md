# Model capability metadata — the `/v1/models` contract

**Normative.** This doc pins the field names, types, and semantics of the per-model capability block served by the llama router (`:4811`) under `meta.llamaswap` in `GET /v1/models`. The blocks live as llama-swap per-model `metadata:` in `llama/config.yaml` (schemaless to llama-swap; the schema is enforced by this doc + review, not by the router). llama-swap merges the block **flat** into `meta.llamaswap`, beside its own reserved keys — so consumers read e.g. `meta.llamaswap.context_length`, and capability fields must never collide with llama-swap's own keys (`aliases`, `type`, `unlisted`, and whatever the pinned llama-swap release reserves).

Landed by `plans/capability-surfacing-and-local-agent-endpoints.md` WP-1 (canary-verified on llama-swap v243, 2026-08-24). Consumers: bench dashboard Serving tab, Open WebUI capability sync (WP-3 script), obsidian-crucible (read-only, programmatic).

## Ground rules

- **Every value must be derivable from, and equal to, the served configuration** — the `cmd:` flags and the live-verified records in `llama/config.yaml`'s own comments. A capability block is a projection of serving facts, never an aspiration. If a `cmd:` changes (context, KV type, MTP, vision), its metadata block changes in the same edit.
- **Semantics are server-path facts, not model-quality claims.** Fields describe what this serving path accepts and does, verified by response body where noted. Task-level *quality* (e.g. tool-call reliability under agentic load) is explicitly out of scope — that is the bake-off harness's and the Forge eval successor plan's territory.
- **Ids vs aliases**: `/v1/models` `id` is the config model **key**; the append-only alias contract lives on `meta.llamaswap.aliases`. Consumers that persist per-model state should prefer aliases where they can choose; consumers that key by `id` (Open WebUI) are recorded in the consumer registry header of `llama/config.yaml`.
- **Enum evolution is append-only**: new levels/values may be added; existing ones are never renamed or repurposed. Per-model *values* are facts and change when a re-verified serving change lands.

## Fields

| Field | Type | Semantics |
| --- | --- | --- |
| `context_length` | int | The served context window: exactly the `-c` value in this model's `cmd:` (the live-verified safe ceiling, not the GGUF header's theoretical max). |
| `input_modalities` | list | What the serving path accepts. Vocabulary: `text`, `image` (`image` requires a loaded `--mmproj`, body-verified). |
| `output_modalities` | list | What the serving path returns. Vocabulary: `text` (chat completion), `embedding` (`--embeddings` path), `score` (`--reranking` path). |
| `tool_calling` | bool | The serving path accepts a `tools` array and emits structured tool calls for this model (llama.cpp `--jinja` template handling; on the native Anthropic `/v1/messages` route this surfaces as `tool_use` blocks — body-verified on qwen38-27b 2026-08-24). Server-path acceptance, not measured task reliability. |
| `reasoning` | enum | Reasoning-trace support **level** — see the enumeration below. |
| `serving.quant` | string | Weight quantization, exactly as named in the backing GGUF filename (e.g. `UD-Q4_K_XL`, `UD-IQ3_XXS`, `F16`). |
| `serving.kv_cache_type` | string | KV cache dtype: `f16` (llama.cpp default; no `-ctk`/`-ctv` flags) or the explicit symmetric quantized type (e.g. `q4_0` = `-ctk q4_0 -ctv q4_0`). Asymmetric K/V types are banned on this fleet (non-fused-attention fallback — see `llama/config.yaml`'s qwen38 ladder record). |
| `serving.flash_attention` | bool | `-fa on` present in `cmd:` (fused-path engagement live-verified per the config's own VRAM-arithmetic records). |
| `serving.mtp` | bool | Speculative multi-token prediction active (`--spec-type draft-mtp`, sidecar `-md` or embedded heads). Lossless (target-verified) — consumers may ignore it functionally; it is a throughput fact. |
| `serving.moe_cpu_offload_layers` | int (optional) | `-ncmoe` value when present — routed-expert weights of the first N layers held in CPU RAM. |
| `serving.ubatch` | int (optional) | Per-model `-ub` override when present (prefill micro-batch; qwen36's PCIe-streaming amortization). |

Optional `serving.*` keys appear only when the underlying flag is present — absence means "macro default", never "unknown".

## `reasoning` support levels

Enumerated, append-only. The level records **observed behavior on this serving path**, live-probed by response body — never inferred from model cards.

| Level | Meaning | Evidence bar |
| --- | --- | --- |
| `none` | No reasoning channel on this serving path (embedding/rerank paths, or a chat model that emits no reasoning trace). | Path shape, or a default-request probe showing no `reasoning_content`. |
| `request-controllable` | Emits a reasoning trace by default (`reasoning_content` on chat completions, `thinking` blocks on `/v1/messages`) **and** a request-level control is live-verified to suppress it: `reasoning_effort: "none"` yields a response with no reasoning trace and intact `content`. | Paired probes (default + `reasoning_effort: "none"`), both body-verified. |
| `always-on` | Emits a reasoning trace and no request-level control is verified to suppress it on this path. | Default probe shows the trace; the suppression probe failed or has not been run. Reserved — no current model carries it. |

Consumer guidance: on a `request-controllable` model, clients that don't want reasoning tokens (or need `content` populated within tight `max_tokens` budgets — the documented reasoning-budget-truncation pattern) should send `reasoning_effort: "none"`. Crucible's vision requests already depend on exactly this for gemma.

### Per-model probe record (2026-08-24, temp 0, body-verified)

| Family (probed alias) | Default request | `reasoning_effort: "none"` | Level |
| --- | --- | --- | --- |
| nemotron-4b | `reasoning_content` present (91 chars), `content` correct | trace absent, `content` correct | `request-controllable` |
| ling-3-tiny (`ling-3-tiny-ud-q4-xl`; quant-invariant: same weights family + template) | present (236 chars), correct | absent, correct | `request-controllable` |
| qwen38-27b (`qwen38-27b-iq3xxs`; quant-invariant) | present (127 chars), correct | absent, correct | `request-controllable` |
| qwen36-a3b | present (448 chars), correct | absent, correct | `request-controllable` |
| gemma-4-12b-qat | reasoning-style template (`max_tokens >= 128` rule) | prior live verification: vision probes with `reasoning_effort: "none"` return clean `content` (2026-08-25 MTP rollout record in `llama/config.yaml`) | `request-controllable` |

## Consumer notes

- **Dashboard** (Serving tab): renders this block per alias, joined with (never replacing) the full parsed-flag table.
- **Open WebUI**: does not discover capabilities from `/v1/models`; the WP-3 sync script pushes them via its admin API, keyed by `/v1/models` `id` (= model key). Registry row in `llama/config.yaml`'s header.
- **obsidian-crucible**: may consume `meta.llamaswap.*` fields named in this doc directly; fields not listed here (llama-swap's own keys, future experiments) carry no stability guarantee. Field names here are append-only-stable per the ground rules.
