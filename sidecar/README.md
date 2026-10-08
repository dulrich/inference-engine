# codex-sidecar

An Anthropic-Messages front end over `codex app-server`. Claude Code talks to the router at `:4813`; the router's `codex-a` / `codex-b` providers talk to this sidecar; the sidecar drives one long-lived `<persona> app-server` child per persona over JSON-RPC on the child's stdio pipe.

Node ≥ 24, `node:*` only, zero npm dependencies.

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
- **The security boundary is a runtime item-type filter, not thread config.** Any `item/started` whose `item.type` is not `agentMessage | reasoning | dynamicToolCall | userMessage` — and any approval request, since `approvalPolicy` is `never` — triggers `turn/interrupt`, an HTTP 502, a `security-event` log line, and marks the persona `degraded`; the child is then killed so the normal restart path brings it back. The filter and the pinned `features.*` flags are **not** env-overridable.
- **The prefix trim is verified, not assumed.** `thread/start` sends the pinned `thread-config.json` (`project_doc_max_bytes: 0` plus twenty `features.*` flags set `false`, `standalone_web_search` among them); the response's `instructionSources` must contain at most the persona `AGENTS.md`, and no hook or MCP-server startup may have fired. Anything else fails closed with a 502 and a `config-unverified` line, because `config` silently ignores unknown keys.
- **The declared feature posture is read back and verified too** (2026-09-14). After those checks, `thread/start` reads `experimentalFeature/list {threadId}` — the app-server's per-thread flag oracle — and any declared key the thread reports with the other value fails the thread closed the same way (`features not honoured: multi_agent=true (declared false)`); so does an oracle that errors (`feature oracle unavailable: …`), never a warning. `unified_exec` is exempt (catalog-pinned true), and a key this binary does not report at all is counted on `unknownFeatures` rather than treated as drift. `config-verified` carries `features`, `unknownFeatures` and `multiAgentMode`. This verifies *config*: WP-1 measured that on codex-cli 0.154.0 these flags do not remove the collab tools or code mode, so the runtime item-type filter above stays the enforcement line and `status.sh` probes the posture at install time.
- **The astra fix is a pinned preamble** (`preamble.txt`, 306 chars) prepended to `baseInstructions` ahead of the client's system text. Not a knob.

## Modules

`codex-sidecar.mjs` is the composition root and the stable entry path (`install.sh` resolves it by name; the systemd unit is generated against it). It reads the config, builds `ctx`, constructs one `Persona` per configured name, binds the servers and owns shutdown — it holds no request logic. Everything else is a focused owner beside it, all `node:*`-only:

| Module | Owns |
| --- | --- |
| `config.mjs` | The pinned inputs (`preamble.txt`, `thread-config.json`, the in-band item-type allowlist) and the environment reading, plus the thread's cwd resolution. |
| `errors.mjs` | `SidecarError` and the upstream `CodexErrorInfo` / `TurnError` / rate-limit-snapshot mapping onto Anthropic error shapes. |
| `logging.mjs` | The day-rolled JSONL log, its retention prune, and the header-redaction rule. |
| `protocol.mjs` | The Anthropic wire boundary as pure functions: model/effort identity, message and system-prompt reading, the session key, the title signature, the codex → Anthropic usage projection and its window clamp. |
| `sinks.mjs` | The two response transports (`BufferSink`, `SseSink`) and the one-shot JSON/error writers. Nothing else writes to a client socket. |
| `session.mjs` | One codex thread's state — thread/turn ids, parked calls, delivered ids, joiners, replayable last outcome, token figures — and the outcome protocol (`waitOutcome` / `settle` / `drain`) the request path awaits. |
| `persona.mjs` | The app-server child: spawn, the newline-framed JSON-RPC codec, pending requests, the thread bus, rate-limit posture, the `tripBoundary` security response, and backoff restart. |
| `thread-config.mjs` | Whether the thread the app-server actually started is the one declared: the instruction-source trim and the declared-vs-reported feature comparison. |
| `request.mjs` | Phase 1–2 of the request path, all pure: `normalizeRequest`, `resolveIdentity`, `requestToken` (the digest that is a request's identity) and `dispositionOf` — the single decision site returning `replay`, `join` or `drive`. |
| `turn.mjs` | Phase 3: `startThread` (with its fail-closed verification), `deliverToolResults`, `startTurn`. |
| `messages.mjs` | `POST /<persona>/v1/messages` as the explicit sequence normalization → session disposition → turn execution → outcome emission. |
| `outbox-events.mjs` | The tracker rows one completed turn produces (`turn.completed`, `child.spawned`, `usage`, `rate_limit`). |
| `http.mjs` | Auth, the closed route table, body reading and the bind-aware `/healthz` payload. |
| `outbox.mjs` | The SQLite event-outbox writer itself. |

## Routes

All routes are authenticated with `authorization: Bearer <SIDECAR_KEY>` **or** `x-api-key: <SIDECAR_KEY>` — including `/healthz` and `/api/hello`. The shared key, not the bind address, is the authorization boundary for bridge peers. The key is never logged. Routes match on the path only, so `?beta=true` is tolerated.

| Route | Behaviour |
| --- | --- |
| `POST /<persona>/v1/messages` | Anthropic Messages, streaming and `stream:false`. `max_tokens` is accepted and ignored. |
| `POST /<persona>/v1/messages/count_tokens` | `ceil(totalChars / 4)` estimate, always 200. Claude Code never calls it in practice. |
| `HEAD\|GET /<persona>/api/hello` | 200 `{}` (Claude Code's pre-flight). |
| `GET /healthz` | `{status, personas:{<persona>:{alive,degraded}}, binds:{expected,bound,missing}, degraded}`; **503** when any persona is degraded **or any expected bind is missing** (`binds.missing` non-empty) — a sidecar the router cannot reach must not read `ok`. |

Errors are Anthropic-shaped `{"type":"error","error":{"type":…,"message":…}}`: 400 `invalid_request_error` (bad JSON, unknown model id, image blocks), 401 `authentication_error` (bad key, upstream auth failure), 404 `not_found_error` (unknown persona or path), 413 (body over 32 MiB), 429 `rate_limit_error` (+ `retry-after` when derivable from `resetsAt`), 502 `api_error`, 503 (degraded persona). In streaming mode an error *after* `message_start` is sent as an `event: error` frame and the stream ends; before it, as an HTTP status.

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

Spike-09 fixture task through the router at `:4813`, six metered `claude -p` runs on 2026-09-12, fixture reset before each: **six green gates**, zero `security-event`, zero `config-unverified`, one locally-answered title query per session (all `envelope: "pinned"`). Detail and caveats: `../docs/tool-calling-gateway/codex-sidecar.md` "Measured"; raw evidence in `../runs/dispatch/codex-appserver-sidecar-wp-4b-report.md`.

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
| `SIDECAR_OUTBOX_PATH` | *(unset)* | SQLite event outbox for the fleet-tools initiative tracker; unset = off. |
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

## Logs

One JSONL file per UTC day, `codex-sidecar-YYYY-MM-DD.jsonl`, mode `0600` inside a `0700` directory. Bearer and `x-api-key` headers are redacted to `<redacted>`. Line kinds worth grepping: `listen` (`binds` = what is bound now, `expected` = what must be), `bind-extra-retrying` / `bind-extra-recovered` / `bind-extra-failed` (the docker0 bind race — one line at first failure, one on success, or one on give-up), `child-started`, `child-exit`, `initialized`, `config-verified`, `config-unverified`, `security-event`, `rate-limited`, `weak-session-key`, `rewind` (a diverged history: `seen`/`now`, plus `at` = first differing message index or `step` = growth size), `fork-local` (a subagent's non-streaming strict-prefix side query answered locally, session untouched: `n`, `seen`), `session-rekeyed` (a subagent session adopted under a new key on lineage proof: `from`, `to`, `threadId`), `rehydrated` (a new thread seeded mid-conversation with the rendered transcript: `nMessages`, `chars`), `title-local`, `parallel-tool-batch`, `effort-changed`, `http-start`, `superseded-request`, `joined-request` (a duplicate request attached to the in-flight turn — a duplicate is one whose SHA-256 digest over model id, effort, system text, tools and all messages matches the owner's, never merely the same shape — computed on the *undecorated* body: `cache_control` keys and harness `text` blocks trailing a `tool_result` are not identity (phase 4, 2026-09-14); `reqDigest` is that digest's first 12 hex chars), `replayed-outcome` (a straggler retry answered from the last completed outcome), `http` (per request; `usage` is the Anthropic projection, `usageCodex` the raw codex delta beside it, and `joinedTo` names the owning request on a joiner's row), `http-error`.

## Running it by hand

```sh
SIDECAR_KEY=$(openssl rand -hex 32) node sidecar/codex-sidecar.mjs
curl -sS -H "authorization: Bearer $SIDECAR_KEY" http://127.0.0.1:4877/healthz
```

Without a real codex binary (no metered turns), point it at the fake app-server:

```sh
printf '%s' '{"defaultTurn":[{"text":"hi"},{"complete":{}}]}' > /tmp/scn.json
SIDECAR_KEY=x SIDECAR_PORT=4899 SIDECAR_BIND_EXTRA= SIDECAR_LOG_DIR=/tmp/sclogs \
  SIDECAR_APP_SERVER_CMD="node sidecar/test/fake-app-server.mjs /tmp/scn.json" \
  node sidecar/codex-sidecar.mjs
```

## Tests

```sh
cd sidecar && npm test          # node --test 'test/**/*.test.mjs'
```

`test/fake-app-server.mjs` is a scripted stdio JSON-RPC stand-in for `codex app-server`: it answers `initialize` / `thread/start` / `turn/start` / `turn/interrupt`, plays per-turn action scripts (deltas, tool-call requests, token usage, delays, `turn/completed`), can forge a `hook/started`, an extra `instructionSources` entry or an out-of-band `commandExecution` item, and can exit mid-turn for the child-death path. Every frame it sees is appended to a trace file so tests can assert on what the sidecar actually sent. No test binds `4877`, spawns a real codex, or touches `~/.config/codex-sidecar`.

## Launch

The sidecar runs as a `systemd --user` service. The unit template lives here (`codex-sidecar.service.in`); `install.sh` renders it with absolute paths, because the systemd user PATH on this host contains neither the nvm node dir nor the persona-wrapper dir.

**Install** (safe to re-run; a second run reports `unchanged`):

```sh
sidecar/install.sh                 # renders ~/.config/systemd/user/codex-sidecar.service
sidecar/install.sh --home /tmp/x   # test render into a throwaway root; skips daemon-reload
```

It resolves `node` (refuses below major 24) and both persona wrappers (`codex-a`, `codex-b`), creates `~/.config/codex-sidecar/` (`0700`) and `env` (`0600`), and generates a `SIDECAR_KEY` **only when the env file is absent** — an existing key is never overwritten. The generated key is printed exactly once, at generation; after that it lives only in the env file and is never echoed again. Nothing is enabled or started: the installer ends by printing the lines below for you to run.

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

**Compose side:** the router reaches the sidecar with the same shared key under the name `CODEX_SIDECAR_KEY` in the fleet-tools `.env` (`hold-for-user`: the user adds `CODEX_SIDECAR_KEY` to that `.env` before the router restart). WP-3 wires the provider entries and `extra_hosts`; until then the key is only consumed here.
