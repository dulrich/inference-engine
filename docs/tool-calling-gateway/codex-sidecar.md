# Codex app-server sidecar — Claude Code over Luna / Sol / Astra

The fourth leg behind the resident router (`:4813`): `codex-a/*` and `codex-b/*` reach a **host** process that speaks Anthropic Messages on the front and `codex app-server` JSON-RPC on the back, so Claude Code can drive the user's codex subscription models with its own tools, permission model and transcript. Built by `plans/codex-appserver-sidecar.md`; the adapter itself is `../../sidecar/` (see `sidecar/README.md` for the operator surface — this doc is the leg's consumer/architecture record).

## Verdict

**The leg is wired and carries real turns; the end-to-end fixture campaign is blocked on one defect.** WP-3's live smoke (2026-09-11) and WP-4's first metered run both reached codex through the full chain and returned real usage. WP-4's run 1 then failed its gate for a reason that is upstream of every model: Claude Code 2.1.269's session-title query no longer matches the sidecar's pinned title signature, so the title query opens the session's codex thread and the real task lands on it as a continuation. Details and evidence: **Known gaps → G1**, and `runs/dispatch/codex-appserver-sidecar-wp-4-report.md`. The measured table below therefore carries one partial row, not six.

## Shape

```
Claude Code ──► router :4813 ──► codex-sidecar :4877 ──► codex-a app-server  (Luna / Sol / Astra)
   (env)          (gateway key)     (shared key)      └──► codex-b app-server
```

- The router providers are plain `type:"anthropic"` entries; the provider `name` doubles as the model-id prefix and the engine strips the first segment, so `codex-a/gpt-5.6-luna` arrives at the sidecar as upstream model `gpt-5.6-luna`. **No engine code and no new patch** in the vendored series — the leg is pure configuration on the router side (`router/AGENTS.md`, "The codex sidecar leg").
- The sidecar is a host process, not a container: it needs the `codex-a` / `codex-b` wrappers and their `$CODEX_HOME`. The router container cannot reach a host `127.0.0.1` socket, so the sidecar binds **both** `127.0.0.1:4877` and the docker0 gateway `172.17.0.1:4877`, and the router service carries `extra_hosts: ["host.docker.internal:host-gateway"]`.
- **The shared key, not the bind address, is the authorization boundary.** Every sidecar route requires `authorization: Bearer <SIDECAR_KEY>` or `x-api-key`, `/healthz` included; the router injects it via `apiKeyEnv: CODEX_SIDECAR_KEY`. Verified 2026-09-11: an unauthenticated `GET /healthz` returns 401.
- One long-lived app-server child per persona, one codex thread per Claude Code session. The adapter **never intermediates the codex credential** — no `auth.json` read, no token in its environment; the child owns its `$CODEX_HOME` and refreshes OAuth itself.

### Consumer profile

These ids are absent from the `/model` picker by design (see G2), so the leg is selected entirely by environment:

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
- **Verification, not assumption.** Every `thread/start` response is checked: `instructionSources` may contain at most the persona `AGENTS.md`, and no `hook/started` or `mcpServer/startupStatus/updated` may have fired. Anything else fails closed — the thread is refused, a `config-unverified` line is logged, the request 502s. Observed live (WP-4 run 1): `instructionSources: ["~/.codex-a/AGENTS.md"]`, `preambleChars: 306`, `config-verified`.

## Runtime execution boundary

Spike 09 found the silent-failure class this closes: with codex's own execution surface available, the model ran commands through `ctx_execute` / `commandExecution` — **outside Claude Code's permission model** — which masked a `Bash` denial in five runs while the run still reported green.

The boundary is therefore enforced at runtime on the frame stream, not inferred from config. Any `item/started` whose `item.type` is not one of `agentMessage | reasoning | dynamicToolCall | userMessage` — and any approval request, since `approvalPolicy` is `never` — triggers `turn/interrupt`, an Anthropic-shaped `api_error` 502, a `security-event` log line, and marks the persona `degraded` (no new threads until the child is restarted). 09 observed `commandExecution` and `mcpToolCall` as the out-of-band types. Neither the filter nor the `features.*` flags are env-overridable.

## Session keying

- **Identity is Claude Code's own `session_id`**, parsed from the body-level `metadata.user_id` JSON string (`{"device_id":…,"account_uuid":…,"session_id":"<uuid>"}`). Body-level matters: it survives the gateway's header allowlist, whereas the `x-claude-code-session-id` header is not forwarded. Fallback when absent is `sha256(system + first user text)` with a `weak-session-key` warning — warned, never silent.
- **Working directory** comes from the `Primary working directory:` line of the `role:"system"` environment message, parsed *before* those entries are filtered out of `messages[]`. Claude Code interleaves `role:"system"` entries mid-conversation; reading them as new turns was 09's other silent failure (extra codex turns, run still green).
- **Rewind** (fewer conversational messages than last seen) starts a fresh thread and logs it; `thread/fork` and `rollback` are not used.
- One consequence is load-bearing and is what G1 trips over: **every request Claude Code makes under one `session_id` shares one codex thread**, including its side queries. A side query that is not short-circuited locally becomes turn 1 of the session's thread.

## Three wire facts corrected against the v2 schema

WP-1 built against the app-server v2 schema and corrected three points that the spike's reading had wrong:

1. **The dynamic tool-call reply is flat `{contentItems, success}`** — not wrapped in a `result`/`output` envelope.
2. **`effort` rides on `turn/start`, not `thread/start`**, and it is sticky for the thread once sent.
3. **Token usage arrives on `thread/tokenUsage/updated`**, asynchronously with respect to the turn frames.

Because (3) lands late, usage in `message_start` is **one call lagged** (the previous call's `tokenUsage.last` is all that is known when the message opens) and is **reconciled at the final `message_delta`** on `turn/completed`, so the session total Claude Code reports equals the codex-side total. Measured exactly equal on WP-4 run 1 (below).

## Measured

Campaign: the spike-09 fixture task (`/tmp/cc-appserver-fixture`, gate `npm test`) run through `:4813` with the flags pinned in spike 09. **Blocked after run 1 by G1** — the remaining five runs were not spent, because the failure is deterministic and upstream of model selection, so re-running it on other models would have burned subscription turns to re-observe the same defect. Full evidence: `runs/dispatch/codex-appserver-sidecar-wp-4-report.md`.

| run | model | gate | first-turn input | usage agreement (sidecar vs `result.usage`) | security-event | config-unverified | title-local | wall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `luna-david` | `codex-a/gpt-5.6-luna` | **red** (G1) | 15,508 | **exact** — 15,508 in / 79 out on both sides, delta 0 | 0 | 0 | **0** (expected 1 — this is G1) | 5.1 s |
| `sol-b`, `astra-b`, `astra-david`, `luna-conc-a`, `luna-conc-b` | — | not run | — | — | — | — | — | — |

Related live evidence, not part of the campaign: WP-3's `smoke-router.sh … --codex` (2026-09-11) drove one `codex-a/gpt-5.6-luna` turn end-to-end for `usage.input_tokens = 6463`, and both derived paths (`/codex-a/v1/messages` and `…/count_tokens`) were confirmed in the sidecar log — the engine forwards a `baseurl` path segment unchanged, so the two-port fallback was never needed.

What run 1 does establish, independent of the gate outcome: the chain carries a real tool-calling request (6 tools converted to `dynamicTools`), the shared-key auth and path derivation work through the container boundary, config verification passes with only the persona `AGENTS.md` in `instructionSources`, the preamble is applied at 306 chars, and the usage reconciliation is exact rather than approximate. **Thread isolation, the per-model gate outcomes, ping coverage and the concurrent-session check remain unmeasured.**

## Known gaps

- **G1 — the session-title short-circuit is coupled to the Claude Code version, and 2.1.269 broke it.** The sidecar answers the title query locally only on a full pinned signature, one clause of which requires the user message to end at `</session>`. 2.1.269 appends a language instruction *after* the closing tag, so the signature misses, the title query opens the session's thread, and the real task arrives as a continuation of a thread whose turn 1 was "name this session" — the model answers the coding task with another title JSON and the gate stays red. Verified against the shipped bundle and by calling the sidecar's own `isTitleRequest` on both shapes (`true` for the spike-era prompt, `false` for 2.1.269's). Fix is a one-clause change in `sidecar/codex-sidecar.mjs`; **not applied here — WP-4 is documentation and does not change adapter code.**
- **G2 — `/model` picker visibility.** Claude Code's model discovery filters on the substrings `claude`/`anthropic`, so `codex-*/…` ids never appear in the picker. Accepted; route by `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_*_MODEL`. No rename.
- **G3 — `count_tokens` is an estimate.** `POST /<persona>/v1/messages/count_tokens` returns `ceil(totalChars / 4)` and always 200; the app-server exposes no tokeniser. Claude Code was never observed calling it in practice.
- **G4 — reasoning items are dropped.** `reasoning` items pass the execution-boundary filter but are not surfaced to Claude Code as thinking blocks.
- **G5 — images are rejected.** An image content block is a 400 `invalid_request_error` with an explicit message; there is no image path to the app-server.
- **G6 — rate-limit and auth frames are mapped from the schema, not observed live.** `account/rateLimits/updated` exhaustion maps to `rate_limit_error` 429 (with `retry-after` when derivable from `resetsAt`) and auth/refresh failure to `authentication_error` 401, so patch 0003's verbatim relay hands Claude Code something its retry logic understands — but neither frame has been seen on the wire. **No live occurrence during WP-4** either; the next campaign should keep recording the first one.
- **G7 — structured-output requests are not honoured.** The title query sends `outputFormat: {type:"json_schema", …}`; the sidecar has no equivalent to pass to the app-server and drops it. Harmless while the title query is short-circuited locally, and a correctness question the moment it is not (see G1).

## Operations

- **Install / launch**: `sidecar/install.sh` renders the `systemd --user` unit with absolute paths (the systemd user PATH on this host has neither the nvm node dir nor the wrapper dir); the user then runs `systemctl --user enable --now codex-sidecar` — the agent cannot. `sidecar/status.sh` prints the unit state plus an authenticated `/healthz`, never the key.
- **Key rotation**: stop the unit, rewrite `~/.config/codex-sidecar/env` (`0600`) with a fresh `SIDECAR_KEY`, start it, then update `CODEX_SIDECAR_KEY` in the fleet-tools `.env` to match and restart the router. The two values must move together — the sidecar 401s anything else.
- **Logs**: one JSONL file per UTC day under `~/.local/state/codex-sidecar/logs/` (`0600` in a `0700` dir), pruned after `SIDECAR_LOG_KEEP_DAYS` (14). Bearers are redacted. Kinds worth grepping when a session misbehaves: `config-verified` / `config-unverified` (prefix trim), `security-event` (execution boundary), `title-local`, `weak-session-key`, `rewind`, `http` (per request: `key` = Claude Code session id, `mode`, `threadId`, `usage`, `toolCalls`).
- **Restart semantics**: a config change on the router side needs a router restart and a fresh `smoke-router.sh` run — a green healthcheck proves nothing here (repo-wide quirk). The sidecar itself is shared and live; do not bounce it while another session is mid-turn.
