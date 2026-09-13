# Resident router (:4813) — consumer guide

The resident mixed local/subscription-Claude gateway shipped by `plans/resident-router-ai-gateway.md` (build context `router/`, fleet wiring in `fleet-tools/compose.fleet.yml` + `SERVICES.md`). Engine: `@the-next-ai/ai-gateway` 1.0.18, source build pinned at `782cd51` with a four-patch vendored series (`router/patches/0001`–`0004`; rationale per patch in `router/AGENTS.md`): 0001 omits `x-api-key` for the passthrough OAuth bearer (the sole defect that blocked subscription passthrough, `oauth-passthrough-probe.md`), 0002 adds the local-leg prefill/idle-gap ping keepalive, 0003 relays upstream error bodies byte-verbatim to Anthropic-protocol clients, 0004 adds the `/v1/messages/count_tokens` route (`plans/router-prefill-keepalive-and-protocol-gaps.md`). Evidence records this doc builds on: `routing-decision.md` (arm selection, qwen38 conversion fix), `oauth-passthrough-probe.md` (OAUTH-PROBE-OK / AUTH-COEXIST-OK).

One loopback port, two Anthropic-protocol legs behind one client-auth boundary:

- **Local leg** — `claude-local/*` → `inference-engine-llama` over the compose network (`:4806` internal). This is the leg that makes `qwen38-27b-iq3s` usable under Claude Code (the engine's Anthropic→OpenAI conversion folds the mid-conversation system message the Qwen3.8 template 500s on).
- **Hosted leg** — pure OAuth passthrough to `api.anthropic.com` for **subscription Claude**. Claude Code owns the OAuth lifecycle (saved claude.ai login); the gateway preserves the bearer + `anthropic-beta` on this leg and strips them on the local leg (provider `apikey` presence/absence is the switch — see `router/AGENTS.md`).
- **OpenRouter tee** (added 2026-08-25, `openrouter-spike.md`) — `openrouter/<or-model-id>` routes via OR's OpenAI-compat endpoint through the same conversion leg as claude-local (keepalive-covered); `or-anthropic/<or-model-id>` routes via OR's native Anthropic-protocol `/api/v1/messages` (higher fidelity: OR cost fields in `usage`, native SSE; requires the client to send *some* bearer — local-mode `ANTHROPIC_AUTH_TOKEN` qualifies; the `or-anthropic/…` ids are also the picker-visible ones under model discovery). Credential is the operator's OpenRouter key via `apiKeyEnv` — never the caller's. `count_tokens` 404s on OR models (OR doesn't serve it).
- **Codex sidecar leg** (added 2026-09-11, `codex-sidecar.md`) — `codex-a/<id>` and `codex-b/<id>` route to a **host** process (`sidecar/`, `systemd --user codex-sidecar`, `:4877`) that speaks Anthropic Messages on the front and `codex app-server` JSON-RPC on the back, putting the user's Luna / Sol / Astra subscription models under Claude Code's own tools and permission model. Plain `type:"anthropic"` providers with no engine change and no new patch: the provider `name` is the model-id prefix the engine strips. The container cannot reach a host `127.0.0.1` socket, so the sidecar also binds docker0 `172.17.0.1` and the router service carries `extra_hosts: ["host.docker.internal:host-gateway"]`. Credential is a shared bridge key via `apiKeyEnv: CODEX_SIDECAR_KEY` — never the caller's bearer — and that key, not the bind address, is the authorization boundary. Effort rides in the id suffix (`gpt-5.6-luna-high`); these ids are **not** picker-visible (same `claude`/`anthropic` substring filter), so select them by `ANTHROPIC_MODEL`/`ANTHROPIC_DEFAULT_*_MODEL`. Smoke: `smoke-router.sh … --codex` (check 7, metered). End-to-end fixture verification is **complete** — six metered `claude -p` fixture runs, six green gates, across three models and both personas including two concurrent same-prompt sessions, with zero execution-boundary and zero config-verification failures; see `codex-sidecar.md` "Measured".

Client auth: static key on the **`x-gateway-key`** header (`INFERENCE_ROUTER_GATEWAY_KEY` in fleet-tools `.env`, gitignored). The `authorization` bearer is an accepted fallback auth candidate in the engine's lookup, which is what makes local mode's `ANTHROPIC_AUTH_TOKEN` form work.

## Consumer profiles

**Subscription mode** (Claude Code on your claude.ai login, routed through the gateway):

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4813
ANTHROPIC_CUSTOM_HEADERS="x-gateway-key: <INFERENCE_ROUTER_GATEWAY_KEY value>"
# NEVER set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY in this mode — either one
# replaces the saved-login OAuth bearer and breaks or re-bills the session.
```

Claude Code keeps its saved login when only `ANTHROPIC_BASE_URL` is set; the gateway passes the OAuth bearer through untouched. Requests use the **bare** model ids (`claude-sonnet-5`, …) exactly as Claude Code sends them — a bare id auto-routes to the `anthropic` provider (verified in `resolveTargetProviders`; the `anthropic/` prefix appears only in the `/v1/models` listing).

**Local mode** (Claude Code fully on local models):

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4813
ANTHROPIC_AUTH_TOKEN=<INFERENCE_ROUTER_GATEWAY_KEY value>
ANTHROPIC_MODEL=claude-local/qwen38-27b-iq3s
ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-local/nemotron-4b
```

The `claude-local/` prefix **is required** on request ids — it is what selects the local provider over the default hosted leg. Keep small models in the haiku slot only (the WP-4 positional-derail finding, `routing-decision.md`).

**Roster mode** (the fleet's default interactive profile: OAuth login, router base URL, the whole roster in the `/model` picker):

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4813
ANTHROPIC_CUSTOM_HEADERS="x-gateway-key: <INFERENCE_ROUTER_GATEWAY_KEY value>"
ENABLE_TOOL_SEARCH=true                 # a custom base URL auto-disables tool search
CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1    # no background title call behind the router
# Still NEVER ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY — OAuth stays the credential.
```

Those four keys live in the `env` block of `~/.claude/settings.json`, so every interactive and FleetView-launched session rides the router — and so does every subagent, which inherits the parent's base URL (there is no per-agent base URL). The picker rows come from a generated `modelPicker` lineup in the same file: `node router/model-picker-lineup.mjs --write --launch-profile` writes both the `env` block and `modelPicker.options`, one labelled row per `<provider>/<model>` id in `gateway.config.json` (e.g. `Sol medium (codex-b)`, `GLM 5.3 (or-anthropic)`, `Qwen3.8 27B (local)`). Hosted `anthropic/claude-*` ids are excluded — they duplicate the built-in rows, and the bare id is what Claude Code sends anyway — and `replaceBuiltInOptions` is left unset so the built-in subscription rows stay first. The lineup needs no discovery and hits no id validation: behind `ANTHROPIC_BASE_URL` Claude Code passes the model string through unchecked, so `codex-a/gpt-5.6-luna` is selectable by name with no rename and no sidecar change.

The last two are corrections for what a custom `ANTHROPIC_BASE_URL` changes underneath Claude Code. Setting a base URL **auto-disables tool search**, so every tool schema loads up front instead of deferring — roughly 10k tokens of extra assembly in every session — and `ENABLE_TOOL_SEARCH=true` overrides that. `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` suppresses the background terminal-title model call, which behind a custom base URL is not guaranteed to honour the haiku alias (anthropics/claude-code#92050, closed as not planned) and can therefore escape to the hosted API. Both are generated, so a hand-edit of either drifts on the next `--write --launch-profile`.

`node router/model-picker-lineup.mjs --check` is the sync gate (exit 1 when `gateway.config.json` and the settings file disagree; same shape as the fleet's `design:verify`) — re-run `--write --launch-profile` after any provider or model-id change. The generator owns `modelPicker.options` wholesale and, under `--launch-profile`, those four `env` keys; it touches nothing else, refuses a settings file that is not canonical 2-space JSON, and never writes `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`.

**Gateway model discovery is the non-OAuth alternative to Roster mode, not a companion to it.** `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` merges the gateway's `/v1/models` list into the picker, but it filters ids on `/(claude|anthropic)/i` (10 of the router's 30 ids survive today) and it **never runs in an OAuth session**: measured 2026-09-12 on Claude Code 2.1.269 (`claude -p --debug` through `:4813`), the debug log says `[gatewayDiscovery] skipped: no credential (ANTHROPIC_AUTH_TOKEN, apiKeyHelper, or API key)`. A credential supplied only through `ANTHROPIC_CUSTOM_HEADERS` also skips — the header is not a credential as far as discovery is concerned. Discovery is therefore a Local-mode feature (the gateway key is `ANTHROPIC_AUTH_TOKEN` there, and the saved login is already replaced); Subscription and Roster mode keep the OAuth bearer and use the generated lineup instead. The flag is not set on this fleet.

**Escape hatch.** Because the `env` block is always on, a down router breaks every session: launch `env -u ANTHROPIC_BASE_URL -u ANTHROPIC_CUSTOM_HEADERS claude` to fall back to the direct hosted path until the container is back.

Containment rules for agent fixtures (deny-rule profile, disposable fixture dirs) are unchanged from WP-4 — see `docs/claude-code-local-profile.md`; note its correction that only `Edit(path)` deny rules cover file-editing tools.

## Protocol coverage (closed by `plans/router-prefill-keepalive-and-protocol-gaps.md`, 2026-08-25)

Three of the four launch limitations are now shipped behavior; one accepted gap remains:

- **Prefill/idle keepalive (patch 0002, shipped)**: whenever ≥15 s pass with no frame on a local-leg streamed response — including before `message_start`, i.e. the whole prefill window — the gateway emits an `event: ping` frame, defusing the WP-4 **300 s silent-abort** condition at extreme local context. Pre-`message_start` ping tolerance was measured on Claude Code **2.1.243** (spike record in the plan doc), not assumed from the protocol reference; re-verify on a major harness upgrade. Live-verified on `:4813` (ping 8.7 s before `message_start` on a cold-model 22k-token prompt, intact completion).
- **Upstream error-body passthrough (patch 0003, shipped)**: on the final target attempt for an Anthropic-protocol client, a non-empty upstream error body is relayed byte-verbatim with the upstream status instead of the engine's `All target providers failed.` envelope — restoring Claude Code's documented fallback-on-upstream-error behavior. Other source protocols, non-final attempts, and empty bodies keep the envelope.
- **`/v1/messages/count_tokens` (patch 0004, shipped)**: proxied on both legs behind the same auth/idempotency prehandlers, with the messages path's own ordered model resolution. Live-verified on the local leg (llama.cpp serves it natively) and the hosted leg (200 through the OAuth passthrough, 2026-08-25).
- **Deferred tool search (`ENABLE_TOOL_SEARCH`, conformance-gated 2026-09-12)**: Claude Code's deferred-tool path survives the router on all three legs. Request 1 carries 12 tools — a fully-schema'd `ToolSearch`, exactly one `defer_loading: true` `DeferredToolPlaceholder`, and the always-on tools — with the deferred tools **absent from `tools`** and their names advertised in prose, in a `role:"system"` message interleaved in `messages[]` (hosted leg: inside the user turn). Discovery grows the array by exactly one fully-schema'd entry that **retains** `defer_loading: true`. Measured saving, within-leg and request-1 `system`+`tools` only: hosted 29,159 → 17,745 (tool block 2.0×), local 15,394 → 8,238, codex 14,549 → 7,875. **Cross-leg token comparison is invalid** — each leg has a different eager catalogue and a different tokenizer. Gates: `router/test/tool-search-conformance.test.mjs` and `router/smoke-tool-search.sh` (see `router/AGENTS.md` "Gates"); full measured record in `tool-search-conformance-spike.md`. The codex leg needed a sidecar fix to get there — see `codex-sidecar.md` "Deferred tool advertisement".
- **Accepted gap (unchanged)**: `?beta=true` query forwarding and open-list header forwarding are absent; both measured harmless for both legs (probe doc).

## Operations

- Config (`router/gateway.config.json`) is bind-mounted read-only; a config change needs `docker compose -f <code-root>/fleet-tools/compose.fleet.yml restart inference-engine-router`, then `router/smoke-router.sh http://127.0.0.1:4813 <key>` green.
- **A new Claude model id** (new release, or a legacy id an active `/model` selection sends) needs a `models` line in the `anthropic` provider + restart — the hosted list is enumerated explicitly because an empty (wildcard) list would also empty the `/v1/models` listing that model discovery depends on.
- **Never set `ANTHROPIC_API_KEY` in the service environment** — with gateway auth enabled the engine prefers a managed key and silently hijacks every subscription request onto API-key billing (`router/AGENTS.md`, compose comment block).
- An image rebuild or restart is never complete on a green healthcheck: run the smoke script (repo-wide quirk — probe by response body).
