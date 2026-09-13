# Codex app-server sidecar — Claude Code over Luna / Sol / Astra

The fourth leg behind the resident router (`:4813`): `codex-a/*` and `codex-b/*` reach a **host** process that speaks Anthropic Messages on the front and `codex app-server` JSON-RPC on the back, so Claude Code can drive the user's codex subscription models with its own tools, permission model and transcript. Built by `plans/codex-appserver-sidecar.md`; the adapter itself is `../../sidecar/` (see `sidecar/README.md` for the operator surface — this doc is the leg's consumer/architecture record).

## Verdict

**The leg is verified end to end: six metered fixture runs, six green gates**, across three models (`gpt-5.6-luna`, `gpt-5.6-sol`, `gpt-6-astra`) and both personas, including two concurrent same-prompt sessions. In every run Claude Code drove the codex subscription model through the full chain with its own tools, read the fixture, edited `src/slug.js` and ran the gate itself. Zero `security-event`, zero `config-unverified`, exactly one locally-answered `title-local` per session (all `envelope: "pinned"`), and every session's first `thread/start` belonged to the coding task (`nTools: 6`), not to a side query. Numbers in **Measured**; the defect that blocked WP-4's first attempt is closed and now fails safe (**Known gaps → G1**). Evidence: `runs/dispatch/codex-appserver-sidecar-wp-4b-report.md`.

## Shape

```
Claude Code ──► router :4813 ──► codex-sidecar :4877 ──► codex-a app-server  (Luna / Sol / Astra)
   (env)          (gateway key)     (shared key)      └──► codex-b app-server
```

- The router providers are plain `type:"anthropic"` entries; the provider `name` doubles as the model-id prefix and the engine strips the first segment, so `codex-a/gpt-5.6-luna` arrives at the sidecar as upstream model `gpt-5.6-luna`. **No engine code and no new patch** in the vendored series — the leg is pure configuration on the router side (`router/AGENTS.md`, "The codex sidecar leg").
- The sidecar is a host process, not a container: it needs the `codex-a` / `codex-b` wrappers and their `$CODEX_HOME`. The router container cannot reach a host `127.0.0.1` socket, so the sidecar binds **both** `127.0.0.1:4877` and the docker0 gateway `172.17.0.1:4877`, and the router service carries `extra_hosts: ["host.docker.internal:host-gateway"]`.
- **The shared key, not the bind address, is the authorization boundary.** Every sidecar route requires `authorization: Bearer <SIDECAR_KEY>` or `x-api-key`, `/healthz` included; the router injects it via `apiKeyEnv: CODEX_SIDECAR_KEY`. Verified 2026-09-11: an unauthenticated `GET /healthz` returns 401.
- One long-lived app-server child per persona, one codex thread per Claude Code agent — (`session_id`, parent-or-subagent scope); see Session keying. The adapter **never intermediates the codex credential** — no `auth.json` read, no token in its environment; the child owns its `$CODEX_HOME` and refreshes OAuth itself.

### Consumer profile

These ids now reach the `/model` picker as generated `modelPicker` rows (G2, resolved 2026-09-12 — `router/model-picker-lineup.mjs`), and they stay selectable by environment for headless runs:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4813
ANTHROPIC_CUSTOM_HEADERS="x-gateway-key: <INFERENCE_ROUTER_GATEWAY_KEY value>"
ANTHROPIC_AUTH_TOKEN=<INFERENCE_ROUTER_GATEWAY_KEY value>   # some client bearer is required
ANTHROPIC_MODEL=codex-a/gpt-5.6-luna
ANTHROPIC_DEFAULT_HAIKU_MODEL=codex-a/gpt-5.6-luna
ANTHROPIC_DEFAULT_SONNET_MODEL=codex-a/gpt-5.6-luna
ANTHROPIC_DEFAULT_OPUS_MODEL=codex-a/gpt-5.6-luna
ANTHROPIC_SMALL_FAST_MODEL=codex-a/gpt-5.6-luna
```

Set **all** of the default-model slots, not just `ANTHROPIC_MODEL`: Claude Code issues side queries (session title, and whatever else it routes to the small/fast slot) that would otherwise leave this leg. Never set `ANTHROPIC_API_KEY` anywhere near the router — with gateway auth on, the engine prefers a managed key and silently re-bills the session.

## Pinned config and preamble

Both are files in the adapter, not knobs, and are reproduced here by reference rather than by copy — read `sidecar/thread-config.json` and `sidecar/preamble.txt` for the authoritative text.

- **`sidecar/thread-config.json`** — the P5 prefix trim: `project_doc_max_bytes: 0` plus nineteen `features.*` flags set `false`. Measured in spike 09 to remove 4,412 of 10,869 prefix tokens (41 %). It is a **token optimisation only**; it is not the security boundary, because `config` silently ignores unknown keys.
- **`sidecar/preamble.txt`** — 306 characters prepended to `baseInstructions` ahead of the client's system text. It exists because `gpt-6-astra` otherwise chases the persona `AGENTS.md` skill references outside the working directory and then declares the environment read-only from `thread/start`'s `sandbox:"read-only"`. Measured in the fix-arms spike: preamble 2/2 green at +58 tokens; a trimmed persona `AGENTS.md` alone 0/2. Pinned, not an env flag.
- **Verification, not assumption.** Every `thread/start` response is checked: `instructionSources` may contain at most the persona `AGENTS.md`, and no `hook/started` or `mcpServer/startupStatus/updated` may have fired. Anything else fails closed — the thread is refused, a `config-unverified` line is logged, the request 502s. Observed live 6/6 in the WP-4b campaign: `instructionSources` holding only the persona `AGENTS.md`, `preambleChars: 306`, `config-verified`; zero `config-unverified`.

## Runtime execution boundary

Spike 09 found the silent-failure class this closes: with codex's own execution surface available, the model ran commands through `ctx_execute` / `commandExecution` — **outside Claude Code's permission model** — which masked a `Bash` denial in five runs while the run still reported green.

The boundary is therefore enforced at runtime on the frame stream, not inferred from config. Any `item/started` whose `item.type` is not one of `agentMessage | reasoning | dynamicToolCall | userMessage` — and any approval request, since `approvalPolicy` is `never` — triggers `turn/interrupt`, an Anthropic-shaped `api_error` 502, a `security-event` log line, and marks the persona `degraded` (no new threads until the child is restarted). 09 observed `commandExecution` and `mcpToolCall` as the out-of-band types. Neither the filter nor the `features.*` flags are env-overridable.

## Session keying

- **Identity is (`session_id`, agent scope) — one codex thread per pair.** The `session_id` is parsed from the body-level `metadata.user_id` JSON string (`{"device_id":…,"account_uuid":…,"session_id":"<uuid>"}`). Body-level matters, and it is now the *only* option: the gateway's header allowlist strips every `x-claude-code-*` header before the sidecar sees the request, so neither `x-claude-code-session-id` nor `x-claude-code-agent-id` is forwarded (measured 2026-09-12, CC 2.1.270). Claude Code hands a subagent its parent's `session_id` by design, so the id alone cannot separate them; the scope witness is the `cc_is_subagent=true;` token inside the `x-anthropic-billing-header:` line that leads `system` (present on every subagent request, never on a parent's). Keys: parent → the bare `session_id`, unchanged, so a mid-session system-prompt change cannot fragment the parent's thread; subagent → `<session_id>:sha256(system-minus-billing-line + "\0" + first user text)[:16]`, both stable across that agent's own turns. `sessionKeyOf` prefers `<session_id>:<agent id>` if the header ever starts arriving, but never requires it. Known limit, accepted: two *concurrent* subagents of one parent with an identical system prompt **and** an identical first prompt still collide on one key. Every `config-verified` and `http` log row carries `scope: "parent" | "subagent"` beside the key. Fallback when no `session_id` is present is `sha256(system + first user text)` with a `weak-session-key` warning — warned, never silent.
- **Working directory** comes from the `Primary working directory:` line of the `role:"system"` environment message, parsed *before* those entries are filtered out of the conversation. Claude Code interleaves `role:"system"` entries mid-conversation; reading them as new turns was 09's other silent failure (extra codex turns, run still green).
- **Those entries leave the turn, but not the thread.** `conversational()` drops every `role:"system"` entry from `messages[]` — correct, they are not turns — and until 2026-09-12 their *text* was discarded with them. Claude Code puts the deferred-tool name list, the agent-type listing and the installed-skills listing **only** in those entries, so the drop silently cost the leg all three. `interleavedSystemText()` now collects them in message order and `baseInstructionsOf()` folds them into `baseInstructions` after `PREAMBLE` and the client's `system` text; with nothing interleaved the string is byte-identical to before. Because it is built once at `thread/start` from inputs that are stable across an agent's own turns, the subagent key above — which hashes **top-level** `system` minus the billing line, not the interleaved messages — is unaffected. See "Deferred tool advertisement".
- **Rewind** (fewer conversational messages than last seen) starts a fresh thread and logs it; `thread/fork` and `rollback` are not used.
- One consequence is load-bearing, and it is what G1 was about: **every request Claude Code makes under one `session_id` *and* one agent scope shares one codex thread**, including its side queries (a session's background calls carry no subagent marker, so they land on the parent's thread exactly as before; only dispatched subagents split off). A side query that is not short-circuited locally becomes turn 1 of the session's thread — which is why the title short-circuit is now written to fail safe rather than to match an exact prompt shape (G1). Measured across the six WP-4b runs: every session's first `thread/start` carried `nTools: 6` and `mode: "new-thread"` for the coding task, and each session's title query was answered locally instead.

## Three wire facts corrected against the v2 schema

WP-1 built against the app-server v2 schema and corrected three points that the spike's reading had wrong:

1. **The dynamic tool-call reply is flat `{contentItems, success}`** — not wrapped in a `result`/`output` envelope.
2. **`effort` rides on `turn/start`, not `thread/start`**, and it takes effect **per turn** — it is not sticky for the thread. Measured 2026-09-13 over one open `gpt-5.6-luna` thread, the same reasoning puzzle three times: `low` → 0 reasoning tokens, `high` on that same thread → 87, `low` again → 0. The thread-level `reasoningEffort` that `thread/start` reports back stays at the model's default throughout and is not the turn's value. The one observable cost of changing effort mid-thread is **one prefix-cache miss on the changed turn** (`cachedInputTokens: 0` where its neighbours cached 3,840/5,888) — not a replay, not a reset. So the sidecar re-sends the resolved effort on every `turn/start` and never opens a new thread for a change; it only logs one `effort-changed` row.
3. **Token usage arrives on `thread/tokenUsage/updated`**, asynchronously with respect to the turn frames.

Because (3) lands late, usage in `message_start` is **one call lagged** (the previous call's `tokenUsage.last` is all that is known when the message opens) and is **reconciled at the final `message_delta`** on `turn/completed`, so what Claude Code reports is the reconciled codex-side figure rather than the lagged one. Measured exact on output tokens in all six WP-4b runs; see **Measured → Usage reconciliation, restated** for what the input columns do and do not mean.

### Effort precedence

Since 2026-09-13 the sidecar resolves the turn's effort from the request, not from the model id alone. Highest precedence first: an explicit **`-low|-medium|-high` id suffix** (`effortSource: "suffix"` — explicit beats implicit, so a suffix wins even against a conflicting request field); else the harness's **`output_config.effort`** (`low`/`medium`/`high` onto themselves, `max` and `xhigh` → `high`; `effortSource: "request"`); else **`thinking.type === "disabled"`** → `low`, the app-server's floor since it has no "off" level (`"request"`); else the **`MODEL_FAMILIES` default** (`effortSource: "default"`). Nothing in this chain can fail a request: an unrecognised `output_config.effort` value, `thinking.type` of `enabled` or `adaptive`, and absent fields all fall to the family default rather than a 400. Two deliberate divergences from the local leg's patch 0007 (`router/AGENTS.md`, "0007"): `disabled` ranks *below* an explicit level here (0007 ranks it first, because that leg has a real off switch), and 0007's `thinking:{type:"enabled", budget_tokens}` → level rung is omitted, because the only client on this leg sends `type:"adaptive"` and a budget→level table would be an unmeasured guess.

The resolved value and its source go on the `config-verified` and `http` rows as `effort` / `effortSource`, and a turn whose effort differs from the previous turn's on the same thread writes one `effort-changed` row (`{persona, threadId, key, from, to, source}`) — a record of the one prefix-cache miss in wire fact 2, and nothing else: no new thread, no interrupt.

## Measured

Campaign (WP-4b, 2026-09-12): the spike-09 fixture task (`/tmp/cc-appserver-fixture`, gate `npm test`, reset before every run) driven by `claude -p` through `:4813` with the flags pinned in spike 09. **Six runs, six green gates** — Claude Code made the edit and ran the gate itself in each. Full evidence: `runs/dispatch/codex-appserver-sidecar-wp-4b-report.md`.

| run | model | effort | gate | turns (sidecar `http` rows) | tool calls relayed | first metered turn input | codex thread cumulative input | output (sidecar / Claude Code) | security-event | config-unverified | title-local | ping | wall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `luna-david` | `codex-a/gpt-5.6-luna` | low | **green** | 9 | 8 | 10,121 | 23,131 (22,016 cached) | 526 / 526 | 0 | 0 | 1 (`pinned`) | 0 | 22.5 s |
| `sol-b` | `codex-b/gpt-5.6-sol` | medium | **green** | 10 | 9 | 11,636 | 28,396 (27,520 cached) | 1,037 / 1,037 | 0 | 0 | 1 (`pinned`) | 0 | 37.1 s |
| `astra-b` | `codex-b/gpt-6-astra` | medium | **green** | 11 | 10 | 12,237 | 27,096 (25,728 cached) | 321 / 321 | 0 | 0 | 1 (`pinned`) | 0 | 26.5 s |
| `astra-david` | `codex-a/gpt-6-astra` | medium | **green** | 11 | 10 | 12,237 | 26,880 (25,472 cached) | 312 / 312 | 0 | 0 | 1 (`pinned`) | 0 | 20.9 s |
| `luna-conc-a` | `codex-a/gpt-5.6-luna` | low | **green** | 5 | 4 | 10,121 | 23,274 (22,016 cached) | 597 / 597 | 0 | 0 | 1 (`pinned`) | 0 | 18.3 s |
| `luna-conc-b` | `codex-a/gpt-5.6-luna` | low | **green** | 12 | 11 | 10,121 | 25,368 (24,064 cached) | 861 / 861 | 0 | 0 | 1 (`pinned`) | 0 | 30.6 s |

What the columns establish:

- **Tool calling is real, on every model.** 52 tool calls relayed as `dynamicTools` across the six runs (`Glob`, `Read`, `Bash`, `Edit`), each `http` row carrying `nTools: 6`. The model never had a path around Claude Code's permission model: `security-event` is 0 campaign-wide, so the execution-boundary filter never had to fire.
- **Config verification passed 6/6**, each with `preambleChars: 306` and `instructionSources` containing only the persona `AGENTS.md` (`~/.codex-a/AGENTS.md` or `…/.codex-b/AGENTS.md`). `config-unverified`: 0.
- **Effort resolves from the bare id**: `gpt-5.6-luna` → `low`, `gpt-5.6-sol` and `gpt-6-astra` → `medium`, sent on `turn/start` (wire fact 2). *(Superseded 2026-09-13 by the precedence rule — a bare id yields the family default only when the request carries no effort; see "Effort precedence". The campaign's six runs predate the rule and sent no `output_config.effort`, so their numbers stand as measured.)*
- **Concurrency is isolated.** `luna-conc-a` and `luna-conc-b` ran the identical prompt against two fixture copies, their threads opened 43 ms apart on the *same* `codex-a` child, and the log shows two distinct `key`s (`050a8bd5…`, `0f1ae67f…`) and two distinct `threadId`s (`01a0935c-4fed-70f2-ae52-d88def4bcf31`, `01a0935c-5018-7d60-955c-57948b682f6a`). Both gates green, both fixtures edited independently.
- **Path forwarding**: only `/codex-a/v1/messages` and `/codex-b/v1/messages` appear — the engine forwards the provider `baseurl` path segment unchanged, so the two-port fallback is not needed. (WP-3's `--codex` smoke also confirmed `…/count_tokens` by the same derivation, at `usage.input_tokens = 6463` for one luna turn.)
- **No keepalive was needed.** Zero `ping` rows and zero `event: ping` in any transcript: no SSE stream went silent for the 15 s `SIDECAR_PING_MS`. The longest gap between consecutive `http` rows in any session — an upper bound on one codex turn plus the local tool round-trip — was 12.97 s (`sol-b`).
- **No live rate-limit or auth frame occurred** in any of the six runs (see G6).

### Usage reconciliation, restated

**Output tokens reconcile exactly, 6/6**: the sum of the sidecar's per-request `usage.output_tokens` equals Claude Code's `result.usage.output_tokens` to the token in every run (the `/` column above), and `modelUsage` attributes it to the `codex-<persona>/<id>` id.

Input is the subtler half, and WP-4's single-turn run could not show it. Because usage arrives on `thread/tokenUsage/updated` asynchronously (wire fact 3), the value logged on a given request is **one call lagged** — the first request of every session logs `0`. Meanwhile codex reports a *cumulative* thread input (the "codex thread cumulative input" column, mostly cache reads), whereas Claude Code's `result.usage.input_tokens` is its own **sum over calls**. The two are different quantities and do not compare directly on a multi-turn session; only on a one-call session (WP-4's run 1) do they coincide. Per-call input values do match: the non-zero inputs in the sidecar log appear verbatim as the corresponding transcript message's `input_tokens`. **Treat the sidecar's cumulative figure as the codex-side truth for subscription burn, and `result.usage.input_tokens` as Claude Code's own per-call accounting — not as a disagreement.**

## Deferred tool advertisement

*Added 2026-09-12 (plan `router-observability-and-tool-search-conformance` WP-6); the defect was measured in WP-5, record: `tool-search-conformance-spike.md`.*

**What was wrong.** With `ENABLE_TOOL_SEARCH=true` Claude Code sends 12 tools — a fully-schema'd `ToolSearch`, exactly one `defer_loading: true` `DeferredToolPlaceholder`, and the always-on tools — and leaves the deferred tools **out of `tools` entirely**, advertising their *names* in prose inside a `role:"system"` message interleaved in `messages[]`. `conversational()` drops every such entry, and nothing re-injected the text, so the sidecar forwarded a perfectly good `ToolSearch` tool to a model that had never been told a single deferred tool exists. The failure was **silent**: no 4xx, no "tool not found", `end_turn` with `is_error: false` and the wrong answer — measured, *"No `NotebookEdit` tool is available in this session."* The same line also discarded the agent-type listing and the installed-skills listing, so the leg's `Agent` and `Skill` tools were impaired by the same defect. `toDynamicTools` was **not** the culprit: the log row for the failing turn carried `nTools: 12`, i.e. nothing was dropped from `tools`.

**The fix.** `interleavedSystemText()` collects the dropped `role:"system"` text in message order; `baseInstructionsOf()` folds it into the thread's `baseInstructions` after `PREAMBLE` and the client's top-level `system` text. Deliberately general — the deferred-tools paragraph is *not* special-cased, which is what also repairs `Agent` and `Skill`. Properties that matter:

- **Byte-stable.** With nothing interleaved the string is byte-identical to the previous `PREAMBLE + system`; existing threads are unchanged.
- **Computed once**, at `thread/start` — a continuation never rebuilds it, so a mid-session advertisement change cannot fragment or re-instruct a live thread.
- **Session keying is untouched.** The subagent discriminator hashes **top-level** `system` minus the billing line plus the first user text; the interleaved messages are not an input to it. No key moves.
- **Observability.** `config-verified` gained `interleavedSystemChars`; the `http` row's `nSystemRole` count is unchanged, and `extractCwd` still reads the environment entry before the filter.

**Gated by** `sidecar/test/tool-search.test.mjs` (advertisement reaches `thread/start`'s `baseInstructions` in message order; a `ToolSearch` → `tool_reference` → grown-catalogue round-trip executes the concrete tool; the no-interleaved-text byte-stability case) and, end to end, `router/smoke-tool-search.sh --codex`.

**Structural limit, not closed by this fix.** Dynamic tools are registered once, in `thread/start`'s `dynamicTools`; `turn/start` carries no tool list and the app-server protocol offers no mid-thread re-registration. When discovery grows Claude Code's catalogue 12 → 13, the new entry's schema therefore does **not** reach an already-open codex thread — the model learns the tool's *name* from the advertisement and its *schema* only from the `ToolSearch` result text relayed as the `tool_result`. Deferred tools whose use needs the formal schema registered may still under-perform on this leg relative to the hosted one. Watch `router/smoke-tool-search.sh --codex` check 4.

## Known gaps

- **G1 (resolved 2026-09-12) — the session-title short-circuit was coupled to the Claude Code version; it now fails safe.** *What broke:* the sidecar answered Claude Code's "name this session" query locally only on a four-clause pinned signature, one clause of which required the user message to **end** at `</session>`. Claude Code 2.1.269 appends a language instruction *after* the closing tag, so the signature missed — and because every request under one `session_id` shares one codex thread, the title query became turn 1 of the session's thread and the real coding task arrived as a continuation of it. The model answered the task with another title JSON, made no edit, and the fixture gate stayed red (WP-4 run 1: `title-local` 0, expected 1). *The fix (`1552357`):* `isTitleRequest` now decides on the three clauses that actually characterise a title query — empty `tools`, a short system prompt carrying the naming phrase verbatim, and a single user message — and the `<session>` envelope was **demoted from a clause to a label**. `titleEnvelope()` records `envelope: "pinned"` or `"drifted"` on the `title-local` log row, so the next time Anthropic rewords the prompt the drift is *observable* rather than silently thread-poisoning: a reworded envelope still short-circuits, and the log says so. *Measured:* 6/6 WP-4b runs logged exactly one `title-local`, all `envelope: "pinned"`, and every session's first `thread/start` carried the coding task's `nTools: 6`. *Monitor:* an `envelope: "drifted"` row is the signal to re-check the signature against the shipped bundle — it is a warning, not a failure. (There is no supported client-side knob to suppress title generation: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` gates plugin downloads, `/bug`, `/feedback` and DesignSync, not `generate_session_title`.)
- **G2 (resolved 2026-09-12) — `/model` picker visibility.** Claude Code's *gateway model discovery* does filter `/v1/models` ids on `/(claude|anthropic)/i`, and it is skipped outright in an OAuth session — but discovery is not the only way into the picker. A `modelPicker.options` lineup in `~/.claude/settings.json`, generated from `gateway.config.json` by `router/model-picker-lineup.mjs --write`, adds one labelled row per `codex-<persona>/<id>` (`Sol medium (codex-b)`, `Astra low (codex-a)`, …) with **no discovery, no id validation** (behind `ANTHROPIC_BASE_URL` Claude Code passes the model string through unchecked) and **no rename** — the stored ids are untouched and the sidecar is unchanged. `model-picker-lineup.mjs --check` is the sync gate after any provider/model-id change; see `resident-router.md` "Roster mode". `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_*_MODEL` routing still works and stays the path for headless runs.
- **G3 — `count_tokens` is an estimate.** `POST /<persona>/v1/messages/count_tokens` returns `ceil(totalChars / 4)` and always 200; the app-server exposes no tokeniser. Claude Code was never observed calling it in practice.
- **G4 — reasoning items are dropped.** `reasoning` items pass the execution-boundary filter but are not surfaced to Claude Code as thinking blocks.
- **G5 — images are rejected.** An image content block is a 400 `invalid_request_error` with an explicit message; there is no image path to the app-server.
- **G6 — rate-limit and auth frames are mapped from the schema, not observed live.** `account/rateLimits/updated` exhaustion maps to `rate_limit_error` 429 (with `retry-after` when derivable from `resetsAt`) and auth/refresh failure to `authentication_error` 401, so patch 0003's verbatim relay hands Claude Code something its retry logic understands — but neither frame has been seen on the wire. **No live occurrence in any of the six WP-4b runs** either; keep recording the first one.
- **G7 — structured-output requests are not honoured.** The title query sends `outputFormat: {type:"json_schema", …}`; the sidecar has no equivalent to pass to the app-server and drops it. Harmless while the title query is short-circuited locally — which it now is on every observed shape (G1) — and a correctness question the moment some other caller asks for structured output.

## Operations

- **Install / launch**: `sidecar/install.sh` renders the `systemd --user` unit with absolute paths (the systemd user PATH on this host has neither the nvm node dir nor the wrapper dir); the user then runs `systemctl --user enable --now codex-sidecar` — the agent cannot. `sidecar/status.sh` prints the unit state plus an authenticated `/healthz`, never the key.
- **Key rotation**: stop the unit, rewrite `~/.config/codex-sidecar/env` (`0600`) with a fresh `SIDECAR_KEY`, start it, then update `CODEX_SIDECAR_KEY` in the fleet-tools `.env` to match and restart the router. The two values must move together — the sidecar 401s anything else.
- **Logs**: one JSONL file per UTC day under `~/.local/state/codex-sidecar/logs/` (`0600` in a `0700` dir), pruned after `SIDECAR_LOG_KEEP_DAYS` (14). Bearers are redacted. Kinds worth grepping when a session misbehaves: `config-verified` / `config-unverified` (prefix trim), `security-event` (execution boundary), `title-local`, `weak-session-key`, `rewind`, `effort-changed` (this turn's effort differs from the previous turn's on the same thread: `from`, `to`, `source`), `http` (per request: `key` = Claude Code session id, `mode`, `threadId`, `effort`/`effortSource`, `usage`, `toolCalls`).
- **Restart semantics**: a config change on the router side needs a router restart and a fresh `smoke-router.sh` run — a green healthcheck proves nothing here (repo-wide quirk). The sidecar itself is shared and live; do not bounce it while another session is mid-turn.
