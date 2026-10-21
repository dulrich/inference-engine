# codex-sidecar

An Anthropic-Messages front end over `codex app-server`. Claude Code talks to the router at `:4813`; the router's `codex-a` / `codex-b` providers talk to this sidecar; the sidecar drives one long-lived `<persona> app-server` child per persona over JSON-RPC on the child's stdio pipe.

A single Rust binary (`rs/`, binary `codex-sidecar`). The black-box test suite under `test/` is Node ≥ 24, `node:*` only, zero npm dependencies.

```
Claude Code ──► router :4813 ──► codex-sidecar :4877 ──► codex-a app-server  (Luna / Sol / Astra)
                                                    └──► codex-b app-server
```

## Shape

- **One child per persona.** The persona name is the provider name, the URL path segment, the `SIDECAR_PERSONAS` entry and the executable spawned (`codex-a app-server`) — all the same string. The wrappers are resolved from `PATH`; no codex path is ever hard-coded.
- **The adapter never intermediates the codex credential.** No `auth.json` read, no token in the environment, no login flow: the child owns its own `$CODEX_HOME` and refreshes OAuth itself. The child's stdin pipe stays open for its lifetime (all JSON-RPC travels over it); only the *service's* inherited stdin is detached (`StandardInput=null` in the unit).
- **Session identity is one codex thread per (`session_id`, agent scope)**, where `session_id` is parsed from the `metadata.user_id` JSON string. Two concurrent sessions never share a codex thread — and neither do a parent and its subagents, which Claude Code gives the *same* `session_id` by design. The scope witness is the `cc_is_subagent=true;` token in the billing line that leads `system`: the `x-claude-code-agent-id` header would be the better discriminator but the gateway's header allowlist strips every `x-claude-code-*` header before the sidecar sees the request, so the sidecar keys on body-level fields only (it will prefer the header if one ever arrives). The parent's key stays the bare `session_id`, so a mid-session system-prompt change cannot fragment its thread; a subagent's key is `<session_id>:sha256(system-minus-billing-line + first user text)[:16]`, both of which are byte-identical across that subagent's own turns. Known limit: two *concurrent* subagents of one parent with an identical system prompt **and** an identical first prompt still share a key. When no `session_id` is present the key falls back to `sha256(system + first user text)` and a `weak-session-key` warning is logged — warned, never silent.
- **Working directory** comes from the `Primary working directory:` line of the `role:"system"` environment message, parsed *before* those entries are filtered out of the conversation. Fallbacks: `SIDECAR_CWD`, then an empty state directory (so no stray `AGENTS.md` is loaded).
- **Interleaved `role:"system"` messages are dropped from the *turn* but their text is kept.** They are never a new turn, so `conversational()` filters them out of `messages[]` — but Claude Code advertises the deferred-tool name list (`ENABLE_TOOL_SEARCH`), the agent-type listing and the installed-skills listing **only** there, never in top-level `system`. `interleavedSystemText()` collects them in message order and `baseInstructionsOf()` folds them into the thread's `baseInstructions` after the preamble and the client's system text. With nothing interleaved the result is byte-identical to the old preamble + system string. It is computed once, at `thread/start`; the subagent discriminator hashes top-level `system` and is unaffected. `config-verified` carries `interleavedSystemChars`, and the `http` row's `nSystemRole` still counts the dropped entries.
- **The security boundary is a runtime item-type filter, not thread config.** Any `item/started` whose `item.type` is not `agentMessage | reasoning | dynamicToolCall | userMessage | contextCompaction` (compaction is codex's own context management, not execution, so it is in band) — and any approval request, since `approvalPolicy` is `never` — triggers `turn/interrupt`, an HTTP 502, a `security-event` log line, and marks the persona `degraded`; the child is then killed so the normal restart path brings it back. The filter and the pinned `features.*` flags are **not** env-overridable.
- **`thread-config.json` sets `model_auto_compact_token_limit: 200000`.** With autocompact off, Claude Code hard-blocks a session at window − 23,000 = 235,400 tokens; compacting the codex thread at 200,000 leaves ~35k headroom below that block. It is a lever, not a guarantee (see `plans/codex-sidecar-context-hygiene.md` Assumptions). The `contextCompaction` items codex emits while compacting are in band, and an all-input-zero `thread/tokenUsage/updated` interim during compaction never replaces the last non-zero usage figure.
- **The prefix trim is verified, not assumed.** `thread/start` sends the pinned `thread-config.json` (`project_doc_max_bytes: 0` plus twenty `features.*` flags set `false`, `standalone_web_search` among them); the response's `instructionSources` must contain at most the persona `AGENTS.md`, and no hook or MCP-server startup may have fired. Anything else fails closed with a 502 and a `config-unverified` line, because `config` silently ignores unknown keys.
- **The declared feature posture is read back and verified too** (2026-09-14). After those checks, `thread/start` reads `experimentalFeature/list {threadId}` — the app-server's per-thread flag oracle — and any declared key the thread reports with the other value fails the thread closed the same way (`features not honoured: multi_agent=true (declared false)`); so does an oracle that errors (`feature oracle unavailable: …`), never a warning. `unified_exec` is exempt (catalog-pinned true), and a key this binary does not report at all is counted on `unknownFeatures` rather than treated as drift. `config-verified` carries `features`, `unknownFeatures` and `multiAgentMode`. This verifies *config*: WP-1 measured that on codex-cli 0.154.0 these flags do not remove the collab tools or code mode, so the runtime item-type filter above stays the enforcement line and `status.sh` probes the posture at install time.
- **The astra fix is a pinned preamble** (`preamble.txt`, 306 chars) prepended to `baseInstructions` ahead of the client's system text. Not a knob.

## Modules

All modules live under `rs/src/`. `main.rs` is the entry point and `sidecar.rs` the composition root; it holds no request logic. Everything else is a focused owner:

| Module | Owns |
| --- | --- |
| `codex` | The typed codex app-server boundary: ids, the usage figure and the frames the sidecar consumes, normalized once at the frame edge (off-schema ids stringified, bad counts read as 0). |
| `config` | The pinned inputs (`preamble.txt`, `thread-config.json`, the in-band item-type allowlist), the strict environment reading, and the thread's cwd resolution. |
| `disposition` | Phase 2 of the request path: classify the request against the persona's sessions, re-key or fork when the history allows, else open a new session. |
| `emission` | Phase 4: the client-facing blocks and finish, the stored last outcome, and the one `http`/`http-error` row per request with its outbox rows. |
| `errors` | `SidecarError` and the upstream `CodexErrorInfo` / `TurnError` / rate-limit-snapshot mapping onto Anthropic error shapes. |
| `http` | Auth (before any body byte is read), the closed route table, the 32 MiB body cap, the bind-aware `/healthz` payload, and the close guard that fires a sink's close hook. |
| `lib` | The crate root: declares the modules. |
| `logging` | The day-rolled JSONL log, its retention prune, and the header-redaction rule. |
| `main` | The binary: boot, print the listening line, graceful shutdown on SIGTERM/SIGINT; any boot failure exits 1. |
| `messages` | `POST /<persona>/v1/messages` as the explicit sequence normalization → session disposition → turn execution → outcome emission, plus the owner/joiner close handling. |
| `outbox` | The event-outbox writer itself: the pinned line format, appended to JSONL day-files. |
| `outbox_events` | The tracker rows one completed turn produces (`turn.completed`, `child.spawned`, `usage`, `rate_limit`). |
| `persona` | The app-server child: spawn, the newline-framed JSON-RPC codec, pending requests, the thread bus, the sessions map, rate-limit posture, the `trip_boundary` security response, and backoff restart. |
| `protocol` | The Anthropic wire boundary as pure functions: model/effort identity, message and system-prompt reading, the session key, the title signature, the codex → Anthropic usage projection and its window clamp. |
| `request` | Phase 1, all pure: the typed request parse (malformed shapes are a 400), `normalize_request`, `request_token` (the digest that is a request's identity), `disposition_of` (the single `replay` / `join` / `drive` decision site), and the transcript rendering. |
| `session` | One codex thread's state — thread/turn ids, parked calls, delivered ids, joiners, replayable last outcome, token figures — and the outcome protocol (`wait_outcome` / `settle` / `drain`) the request path awaits. |
| `sidecar` | The composition root: the process-wide `World` behind one mutex, boot (one `Persona` per configured name), the two binds with the docker0 retry budget, and shutdown. |
| `sinks` | The response transport (`Sink`, buffered or SSE), the message envelope and the one-shot writers. Nothing else writes to a client. |
| `thread_config` | Whether the thread the app-server actually started is the one declared: the instruction-source trim and the declared-vs-reported feature comparison. |
| `turn` | Phase 3: `start_thread` (with its fail-closed verification), `deliver_tool_results`, `start_turn`, and the `still_mine` supersession check. |

## Routes

All routes are authenticated with `authorization: Bearer <SIDECAR_KEY>` **or** `x-api-key: <SIDECAR_KEY>` — including `/healthz` and `/api/hello`. The shared key, not the bind address, is the authorization boundary for bridge peers. The key is never logged. Routes match on the path only, so `?beta=true` is tolerated.

| Route | Behaviour |
| --- | --- |
| `POST /<persona>/v1/messages` | Anthropic Messages, streaming and `stream:false`. `max_tokens` is accepted and ignored. |
| `POST /<persona>/v1/messages/count_tokens` | `ceil(totalChars / 4)` estimate, always 200. Claude Code never calls it in practice. |
| `HEAD\|GET /<persona>/api/hello` | 200 `{}` (Claude Code's pre-flight). |
| `GET /healthz` | `{status, personas:{<persona>:{alive,degraded}}, binds:{expected,bound,missing}, degraded}`; **503** when any persona is degraded **or any expected bind is missing** (`binds.missing` non-empty) — a sidecar the router cannot reach must not read `ok`. |

Errors are Anthropic-shaped `{"type":"error","error":{"type":…,"message":…}}`: 400 `invalid_request_error` (bad JSON, unknown model id, image blocks), 401 `authentication_error` (bad key, upstream auth failure), 404 `not_found_error` (unknown persona or path), 413 (body over 32 MiB), 429 `rate_limit_error` (+ `retry-after` when derivable from `resetsAt`), 502 `api_error`, 503 (degraded persona). In streaming mode an error *after* `message_start` is sent as an `event: error` frame and the stream ends; before it, as an HTTP status.

**Context overflow.** A codex `contextWindowExceeded` error, or any app-server error whose message contains `Input exceeds the maximum length`, becomes 400 `invalid_request_error` with message `prompt is too long: N tokens > M maximum` — the shape Claude Code's reactive compaction recognises. M is the thread's `modelContextWindow`, else `SIDECAR_CONTEXT_WINDOW_TOKENS`; N is the request's `count_tokens` estimate, floored at M+1. After `message_start` the same error object goes out as the `event: error` frame. Not retried; the original codex text is kept in the `http-error` row as `upstreamMessage`.

**Rehydration cap.** The transcript a new thread is seeded with (`rehydrated`) is capped at 240,000 UTF-16 code units, wrapper and marker included. Over budget, prior messages are dropped oldest-first and `<prior-conversation>` opens with `[K earlier message(s) omitted: transcript capped at 240000 characters]`; the final message is always whole (if it alone exceeds the cap, every prior message is elided and the overflow mapping above covers codex's rejection). Under budget the render is unchanged.

Streaming emits `message_start`, `content_block_start/delta/stop`, `message_delta`, `message_stop`, plus `event: ping` (`{"type":"ping"}`) after every 15 s of silence, repeating — native anthropic legs are raw byte relays, so the sidecar must keep itself inside Claude Code's 300 s watchdog. `stop_reason` is `tool_use` when tool calls are relayed, otherwise `end_turn`.

Usage on the wire is **this turn's context occupancy, never cumulative spend** (2026-09-14): the final `message_delta` carries the latest `tokenUsage.last` codex reported for the turn, and `message_start` carries the same quantity one call lagged (the previous call's `last` is all that is known when the message opens, so a session's first message reports `0`). What leaves is the Anthropic projection (`input_tokens` is the *uncached* prompt share, clamped at 0), backstopped by a clamp: if the three input columns would sum above the thread's window — `modelContextWindow` when the app-server reports one, else `SIDECAR_CONTEXT_WINDOW_TOKENS` (default 258,400) — they are scaled down proportionally to sum to exactly the window, `output_tokens` untouched, and a `usage-clamped` row is logged. **Cumulative spend leaves by two other doors only:** the outbox `kind: "usage"` row and the `http` log row's `usageTotalCodex` (`usageCodex` beside it is the raw per-turn figure). See `docs/tool-calling-gateway/codex-sidecar.md` "Usage reconciliation, restated".

## Model ids

`models` on the router provider lists exactly what the sidecar accepts; anything else is a 400.

| id | model | effort |
| --- | --- | --- |
| `gpt-5.6-luna`, `gpt-6-luna` | luna | low (family default — only when the request carries no `output_config.effort`) |
| `gpt-5.6-sol`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6.1-sol` | sol / astra | medium (family default — only when the request carries no `output_config.effort`) |
| `…-low`, `…-medium`, `…-high` | as named | as named (an explicit suffix wins over the request) |

**Effort precedence**, per turn, highest first: the id suffix (`effortSource: "suffix"`) → the request's `output_config.effort` (`low`/`medium`/`high` onto themselves, `max`/`xhigh` → `high`; `"request"`) → `thinking:{"type":"disabled"}` → `low`, the app-server's floor (`"request"`) → the family default (`"default"`). An unrecognised value or an absent field falls to the default and is never a 400. The resolved value rides on every `turn/start` and applies to that turn only; a change mid-thread is logged as `effort-changed` and costs one prefix-cache miss. Full rule and the measurement: `../docs/tool-calling-gateway/codex-sidecar.md` "Effort precedence".

## Measured (WP-4)

Spike-09 fixture task through the router at `:4813`, six metered `claude -p` runs on 2026-09-12, fixture reset before each: **six green gates**, zero `security-event`, zero `config-unverified`, one locally-answered title query per session (all `envelope: "pinned"`). Detail, caveats and evidence: `../docs/tool-calling-gateway/codex-sidecar.md` "Measured".

| run | model | effort | gate | turns | tool calls | codex cumulative input | output (sidecar / Claude Code) | security-event | config-unverified | title-local | wall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `luna-david` | `codex-a/gpt-5.6-luna` | low | green | 9 | 8 | 23,131 | 526 / 526 | 0 | 0 | 1 (`pinned`) | 22.5 s |
| `sol-b` | `codex-b/gpt-5.6-sol` | medium | green | 10 | 9 | 28,396 | 1,037 / 1,037 | 0 | 0 | 1 (`pinned`) | 37.1 s |
| `astra-b` | `codex-b/gpt-6-astra` | medium | green | 11 | 10 | 27,096 | 321 / 321 | 0 | 0 | 1 (`pinned`) | 26.5 s |
| `astra-david` | `codex-a/gpt-6-astra` | medium | green | 11 | 10 | 26,880 | 312 / 312 | 0 | 0 | 1 (`pinned`) | 20.9 s |
| `luna-conc-a` | `codex-a/gpt-5.6-luna` | low | green | 5 | 4 | 23,274 | 597 / 597 | 0 | 0 | 1 (`pinned`) | 18.3 s |
| `luna-conc-b` | `codex-a/gpt-5.6-luna` | low | green | 12 | 11 | 25,368 | 861 / 861 | 0 | 0 | 1 (`pinned`) | 30.6 s |

`luna-conc-a` / `luna-conc-b` ran concurrently with the identical prompt on two fixture copies and kept two distinct session keys and two distinct `threadId`s on the same persona child.

## Environment

Integer variables (ports, `*_MS`, days, tokens, here and under Test seams) are strict: surrounding whitespace is trimmed, unset or empty takes the default, and anything other than plain decimal digits in range (a sign, fraction, exponent, hex, overflow) stops startup with `codex-sidecar: <VAR>: invalid integer "…"`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SIDECAR_KEY` | *(required)* | Shared bridge key. Startup fails without it. Never logged. |
| `SIDECAR_PORT` | `4877` | Loopback listen port. |
| `SIDECAR_BIND_EXTRA` | `172.17.0.1` | Second bind (the docker0 gateway, so the router container can reach it). Empty string skips it. At boot docker0 may not exist yet (`docker.service` started 3.6 s after this unit on 2026-09-14 and the bind failed `EADDRNOTAVAIL`; a user unit cannot order after a system unit, and `network.target` does not exist in the user manager) — so a failed extra bind is retried in the background and `/healthz` reports `degraded` until it lands. |
| `SIDECAR_BIND_RETRY_MS` | `500` | First retry delay for the extra bind; doubles per attempt to a 5 s cap. |
| `SIDECAR_BIND_RETRY_BUDGET_MS` | `60000` | Total wall-clock the extra-bind retry may spend before giving up (`bind-extra-failed`); health stays `degraded` after a give-up. |
| `SIDECAR_PERSONAS` | `codex-a,codex-b` | Personas to serve; each is spawned as `<persona> app-server`. |
| `SIDECAR_CWD` | *(unset)* | Fallback working directory when the request carries no environment block. |
| `SIDECAR_LOG_DIR` | `~/.local/state/codex-sidecar/logs` | JSONL log directory (`0700`, files `0600`). |
| `SIDECAR_LOG_KEEP_DAYS` | `14` | Log retention; pruned at startup and at each UTC day rollover. |
| `SIDECAR_STATE_DIR` | `~/.local/state/codex-sidecar` | State root (holds `empty-cwd/`). |
| `SIDECAR_OUTBOX_PATH` | *(unset)* | Legacy event-outbox path for the fleet-tools initiative tracker; rows are appended to `<dirname>/log/<YYYY-MM-DD>.jsonl` day-files beside it (the `events.db` itself is never written). Unset = off. |
| `SIDECAR_CONTEXT_WINDOW_TOKENS` | `258400` | Fallback window for the wire-usage clamp (catalog 272,000 at the 95% auto-compact scope limit). Used only when the app-server reports no `modelContextWindow` for the thread. |

`ANTHROPIC_API_KEY` is never read, set or forwarded. The execution-boundary item filter and the pinned `features.*` flags have no environment override by design.

### Test seams

These exist only so the suite can run without a real codex binary and without waiting on wall-clock intervals. None of them weakens an invariant — the item-type filter, the fail-closed config check and the auth check are unaffected by every one of them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SIDECAR_APP_SERVER_CMD` | *(unset)* | Command line substituted for the persona wrapper; `{persona}` is replaced with the persona name and `app-server` is still appended, so the real spawn path is exercised. Used to point at `test/fake-app-server.mjs`. |
| `SIDECAR_PING_MS` | `15000` | SSE silence-ping interval. |
| `SIDECAR_BATCH_MS` | `200` | Window for collecting parallel tool calls into one message. |
| `SIDECAR_TURN_TIMEOUT_MS` | `300000` | How long a turn may run before the request fails 502. It bounds the `thread/start` and `turn/start` JSON-RPC awaits too, so an app-server that accepts a request and never answers it fails the HTTP request at the same deadline instead of stalling it forever. |
| `SIDECAR_BACKOFF_MS` | `1000` | Child restart backoff floor (doubles to a 30 s cap; resets after the child survives 60 s). |

## Rust binary

`rs/` is the implementation (`codex-sidecar` binary; ported from the retired Node sidecar by `plans/codex-sidecar-rust-port.md`, Node retired by `plans/codex-sidecar-context-hygiene.md` WP-1). Install it with `sidecar/install.sh`, which runs `cargo build --release --manifest-path sidecar/rs/Cargo.toml` from this checkout, installs the binary atomically to `~/.local/lib/codex-sidecar/codex-sidecar` (previous build kept as `codex-sidecar.prev`) and renders `ExecStart=` to that path with no node directory on the unit `PATH`; `--rust-bin <path>` (or `SIDECAR_RUST_BIN`) installs a prebuilt binary instead. The installed binary reads the pinned files (`preamble.txt`, `thread-config.json`) from the checkout it was built from; moving the checkout requires a reinstall. It never enables or restarts anything: run `systemctl --user daemon-reload && systemctl --user restart codex-sidecar.service` yourself. Rollback: restore `codex-sidecar.prev` over the binary and check out the pinned files from the known-good commit, then the same reload + restart (`AGENTS.md` Rollback).

## Logs

One JSONL file per UTC day, `codex-sidecar-YYYY-MM-DD.jsonl`, mode `0600` inside a `0700` directory. Bearer and `x-api-key` headers are redacted to `<redacted>`. Line kinds worth grepping: `listen` (`binds` = what is bound now, `expected` = what must be), `bind-extra-retrying` / `bind-extra-recovered` / `bind-extra-failed` (the docker0 bind race — one line at first failure, one on success, or one on give-up), `child-started`, `child-exit`, `initialized`, `config-verified`, `config-unverified`, `security-event`, `rate-limited`, `weak-session-key`, `rewind` (a diverged history: `seen`/`now`, plus `at` = first differing message index or `step` = growth size), `fork-local` (a subagent's non-streaming strict-prefix side query answered locally, session untouched: `n`, `seen`), `session-rekeyed` (a subagent session adopted under a new key on lineage proof: `from`, `to`, `threadId`), `rehydrated` (a new thread seeded mid-conversation with the rendered transcript: `nMessages`, `chars`), `title-local`, `parallel-tool-batch`, `effort-changed`, `http-start`, `superseded-request`, `joined-request` (a duplicate request attached to the in-flight turn — a duplicate is one whose SHA-256 digest over model id, effort, system text, tools and all messages matches the owner's, never merely the same shape — computed on the *undecorated* body: `cache_control` keys and harness `text` blocks trailing a `tool_result` are not identity (phase 4, 2026-09-14); `reqDigest` is that digest's first 12 hex chars), `replayed-outcome` (a straggler retry answered from the last completed outcome), `http` (per request; `usage` is the Anthropic projection, `usageCodex` the raw codex delta beside it, and `joinedTo` names the owning request on a joiner's row), `http-error`.

## Running it by hand

```sh
cargo build --release --manifest-path sidecar/rs/Cargo.toml
SIDECAR_KEY=$(openssl rand -hex 32) sidecar/rs/target/release/codex-sidecar
curl -sS -H "authorization: Bearer $SIDECAR_KEY" http://127.0.0.1:4877/healthz
```

Without a real codex binary (no metered turns), point it at the fake app-server:

```sh
printf '%s' '{"defaultTurn":[{"text":"hi"},{"complete":{}}]}' > /tmp/scn.json
SIDECAR_KEY=x SIDECAR_PORT=4899 SIDECAR_BIND_EXTRA= SIDECAR_LOG_DIR=/tmp/sclogs \
  SIDECAR_APP_SERVER_CMD="bun sidecar/test/fake-app-server.mjs /tmp/scn.json" \
  sidecar/rs/target/release/codex-sidecar
```

## Tests

```sh
cargo build --release --manifest-path sidecar/rs/Cargo.toml
cd sidecar && bun run test      # node --test 'test/**/*.test.mjs'
```

The suite spawns `rs/target/release/codex-sidecar` out-of-process by default (a missing binary is a loud error, never a skip); `SIDECAR_TARGET` (a command line, relative to `sidecar/`) overrides it. Pure-function and serialization cases live in the crate's own tests (`rs/tests/*.rs`, `cargo test`).

`test/fake-app-server.mjs` is a scripted stdio JSON-RPC stand-in for `codex app-server`: it answers `initialize` / `thread/start` / `turn/start` / `turn/interrupt`, plays per-turn action scripts (deltas, tool-call requests, token usage, delays, `turn/completed`), can forge a `hook/started`, an extra `instructionSources` entry or an out-of-band `commandExecution` item, and can exit mid-turn for the child-death path. Every frame it sees is appended to a trace file so tests can assert on what the sidecar actually sent. No test binds `4877`, spawns a real codex, or touches `~/.config/codex-sidecar`.

## Launch

The sidecar runs as a `systemd --user` service. The unit template lives here (`codex-sidecar.service.in`); `install.sh` renders it with absolute paths, because the systemd user PATH on this host does not contain the persona-wrapper dir.

**Install** (safe to re-run; a second run reports `unchanged`):

```sh
sidecar/install.sh                 # renders ~/.config/systemd/user/codex-sidecar.service
sidecar/install.sh --home /tmp/x   # test render into a throwaway root; skips daemon-reload
```

It builds and installs the binary (see [Rust binary](#rust-binary)), resolves both persona wrappers (`codex-a`, `codex-b`), creates `~/.config/codex-sidecar/` (`0700`) and `env` (`0600`), and generates a `SIDECAR_KEY` **only when the env file is absent** — an existing key is never overwritten. The generated key is printed exactly once, at generation; after that it lives only in the env file and is never echoed again. Nothing is enabled or started: the installer ends by printing the lines below for you to run. Under the fleet-env cutover switch (`fleet-env enabled`; transitional default off, which is everything above) the installer instead renders `EnvironmentFile=%t/fleet-env/codex-sidecar.env` with `After=`/`Requires=fleet-env-project.service`, checks `CODEX_SIDECAR_KEY` is present via `fleet-env run sidecar-install` (never printed) and the projection exists, and creates no legacy env file; `status.sh` reads `fleet-env path codex-sidecar`. Post-cutover the store is the only copy of the key.

The fleet value for `SIDECAR_OUTBOX_PATH` is `<code-root>/fleet-tools/data/outbox/events.db`; it is added to `~/.config/codex-sidecar/env` by hand and takes effect on `systemctl --user restart codex-sidecar`.

**Enable** (`hold-for-user` — the agent cannot do this):

```sh
loginctl enable-linger $USER                    # only if install.sh reports linger NOT enabled
systemctl --user enable --now codex-sidecar
```

**Verify:**

```sh
sidecar/status.sh
```

`status.sh` prints `systemctl --user status codex-sidecar` (tolerating a not-installed unit), then an authenticated `GET /healthz` on **every** expected bind — loopback and `SIDECAR_BIND_EXTRA` (read from the env file; default `172.17.0.1`, empty skips it) — status code and body, never the key. It asserts on the body (`"status":"ok"`), and exits non-zero on a 503 (a degraded persona or a missing bind), a non-`ok` body, or a refused connection on any bind. Probing loopback alone was blind to the docker0 bind race by construction.

**Rotate the key:**

```sh
systemctl --user stop codex-sidecar
printf 'SIDECAR_KEY=%s\n' "$(openssl rand -hex 32)" > ~/.config/codex-sidecar/env
chmod 0600 ~/.config/codex-sidecar/env
systemctl --user start codex-sidecar
```

Then update the compose-side value to match.

**Compose side:** the router reaches the sidecar with the same shared key under the name `CODEX_SIDECAR_KEY` in the fleet-tools `.env` (post-cutover: the fleet-env store, one copy for both sides) (`hold-for-user`: the user adds `CODEX_SIDECAR_KEY` to that `.env` before the router restart). The router's provider entries (`router/gateway.config.json`) and the compose `extra_hosts` consume it.
