# Resident router (:4813) — consumer guide

The resident mixed local/subscription-Claude gateway shipped by `plans/resident-router-ai-gateway.md` (build context `router/`, fleet wiring in `fleet-tools/compose.fleet.yml` + `SERVICES.md`). Engine: `@the-next-ai/ai-gateway` 1.0.18, source build pinned at `782cd51` with one vendored patch (`router/patches/0001-omit-x-api-key-for-passthrough-bearer.patch` — omit `x-api-key` when the credential is a passthrough OAuth bearer; the sole defect that blocked subscription passthrough, `oauth-passthrough-probe.md`). Evidence records this doc builds on: `routing-decision.md` (arm selection, qwen38 conversion fix), `oauth-passthrough-probe.md` (OAUTH-PROBE-OK / AUTH-COEXIST-OK).

One loopback port, two Anthropic-protocol legs behind one client-auth boundary:

- **Local leg** — `claude-local/*` → `inference-engine-llama` over the compose network (`:4806` internal). This is the leg that makes `qwen38-27b-iq3s` usable under Claude Code (the engine's Anthropic→OpenAI conversion folds the mid-conversation system message the Qwen3.8 template 500s on).
- **Hosted leg** — pure OAuth passthrough to `api.anthropic.com` for **subscription Claude**. Claude Code owns the OAuth lifecycle (saved claude.ai login); the gateway preserves the bearer + `anthropic-beta` on this leg and strips them on the local leg (provider `apikey` presence/absence is the switch — see `router/AGENTS.md`).

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

Both modes: add `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` to surface the merged `/v1/models` list in the model picker (discovery only lists ids containing `claude`/`anthropic` — both legs' ids qualify by construction). Containment rules for agent fixtures (deny-rule profile, disposable fixture dirs) are unchanged from WP-4 — see `docs/claude-code-local-profile.md`; note its correction that only `Edit(path)` deny rules cover file-editing tools.

## Limitations (deliberate, recorded at plan time)

- **No capability fallback**: the engine wraps upstream errors in its own envelope (`401 All target providers failed.`-style), so Claude Code's documented fallback-on-upstream-error behavior is unsupported through this service.
- **No `/v1/messages/count_tokens` route** — 404s through the gateway; Claude Code degrades gracefully (observed across every WP-5 harness run).
- **No prefill keepalive**: no early bytes or pings during upstream prefill, so the WP-4 **300 s silent-abort** condition still applies at extreme local context.
- `?beta=true` query forwarding and open-list header forwarding are absent; both measured harmless for both legs (probe doc).

## Operations

- Config (`router/gateway.config.json`) is bind-mounted read-only; a config change needs `docker compose -f <code-root>/fleet-tools/compose.fleet.yml restart inference-engine-router`, then `router/smoke-router.sh http://127.0.0.1:4813 <key>` green.
- **A new Claude model id** (new release, or a legacy id an active `/model` selection sends) needs a `models` line in the `anthropic` provider + restart — the hosted list is enumerated explicitly because an empty (wildcard) list would also empty the `/v1/models` listing that model discovery depends on.
- **Never set `ANTHROPIC_API_KEY` in the service environment** — with gateway auth enabled the engine prefers a managed key and silently hijacks every subscription request onto API-key billing (`router/AGENTS.md`, compose comment block).
- An image rebuild or restart is never complete on a green healthcheck: run the smoke script (repo-wide quirk — probe by response body).
