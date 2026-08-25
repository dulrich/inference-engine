# Subscription-Claude OAuth passthrough through `@the-next-ai/ai-gateway` — live probe record

Successor measurement to `routing-decision.md` (WP-5 of `plans/capability-surfacing-and-local-agent-endpoints.md`), run 2026-08-25 before drafting the resident-router plan. Motivating research: `references/router-claude-gateway.md` (user-supplied cross-check against Anthropic's Gateway protocol reference), whose central question this probe answers. All observations live-measured on this host: ai-gateway **1.0.18** (npm artifact + source clone at tag commit `782cd51` — identical to the WP-5 artifact), Claude Code **2.1.243** headless (`claude -p`), node v24.15.0. Throwaway rig under `/tmp` (loopback binds 3456/3457/3458), fully torn down at close; no port claim, no service, no credential ever at rest (verified by grep — the only `sk-ant` hits were the diagnostic sink's own prefix-classifier string literals).

## Verdict

**Subscription-Claude mixed routing through the ai-gateway engine works end-to-end, with Claude Code owning the OAuth lifecycle — blocked today by exactly one small engine behavior (the `x-api-key` duplication), which a one-line-class patch fixes.** The formerly-planned "OAuth injection via another library / codexOauth-analogue plugin" spike is **dead**: no injection is needed, Anthropic's documented saved-login-through-gateway flow (`ANTHROPIC_BASE_URL` set, no gateway credential variable) is real and was exercised live against api.anthropic.com on this account's subscription.

Three probe results, in order:

1. **`OAUTH-PROBE-OK`** — Claude Code (saved claude.ai login, `ANTHROPIC_BASE_URL` only) → ai-gateway (anthropic passthrough provider, no configured `apikey`) → thin shim stripping only `x-api-key` → `api.anthropic.com` → 200 `text/event-stream`, correct completion, billed to subscription.
2. **`AUTH-COEXIST-OK`** — same flow with **gateway client auth enabled**: static key on custom header `x-gateway-key` (`AUTH_STATIC_API_KEY_HEADER=x-gateway-key`, `AUTH_STATIC_API_KEY_BEARER_ONLY=false`), supplied by Claude Code via `ANTHROPIC_CUSTOM_HEADERS="x-gateway-key: <key>"`. Missing/wrong key → clean 401 at the gateway; correct key → OAuth bearer passes through untouched, upstream 200.
3. **Failure attribution proven** — without the shim, the same request 401s. Header capture at both hops (redacting sink; category+length only, never token bytes) shows why: the gateway re-emits the resolved credential as **both** `authorization: Bearer <token>` **and** `x-api-key: <token>` (`buildAnthropicHeaders`, `src/adapters/builtins/common.ts:61-73` — deliberate, for Ollama-Cloud-style OpenAI-compat endpoints). An OAuth token in `x-api-key` is an invalid API key, and api.anthropic.com evaluates it → 401. Removing only that header makes the identical request succeed.

## What Claude Code actually sends (saved-login + base-URL mode, captured)

`POST /v1/messages?beta=true` with:

- `authorization: Bearer sk-ant-oat…` (OAuth token, len 108) — **no `x-api-key`**
- `anthropic-beta: oauth-2025-04-20,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,context-management-2025-06-27,prompt-caching-scope-2026-01-05,claude-code-20250219,advisor-tool-2026-03-01,extended-cache-ttl-2025-04-11` — the OAuth capability leads the list, exactly as the protocol reference says
- `anthropic-version: 2023-06-01`, `anthropic-dangerous-direct-browser-access`, `x-app`, `x-claude-code-session-id`, `x-stainless-*`
- With `ANTHROPIC_CUSTOM_HEADERS` set, the custom header rides alongside — confirming the gateway-auth coexistence mechanism.

## What the gateway forwards (passthrough path, captured)

- `authorization` bearer **passes through** ✔ (when no managed key is configured — see credential-resolution note below)
- `anthropic-beta` and `anthropic-version` forwarded intact ✔
- Body forwarded at identical length (78565 bytes) ✔ — JSON-structural passthrough, system-array position preserved
- **`x-api-key` duplicated from the bearer** ✘ — the sole 401 cause (fixed by the shim; the resident plan's patch point)
- `?beta=true` query **dropped** — measured harmless (upstream 200 without it), but noncompliant with forward-verbatim guidance
- `x-app`, `x-claude-code-session-id`, `anthropic-dangerous-direct-browser-access`, `x-stainless-*` **dropped** (the engine forwards an allowlist, not the protocol reference's open list) — measured harmless for a basic call

## Source findings that shape the resident-router plan (all at tag `782cd51`)

- **Route-dependent credential handling already exists — no fork needed for the core mechanism.** Passthrough headers resolve the upstream credential as `x-api-key || incoming bearer || managed key` (`common.ts:50-56`); then `applyProviderRequestOverrides` → `applyProviderCredentials` (`src/gateway/handler.ts`) **replaces** the credential for any provider with `apikey` configured. So a hosted Anthropic provider **without** `apikey` passes the OAuth bearer through, while `claude-local` (with `apikey`) strips/replaces it before llama-swap — the exact architecture in `references/router-claude-gateway.md`.
- **Credential-resolution precedence caveat**: with gateway auth enabled (static/introspection), `shouldPreferManagedCredential` (`common.ts:161-168`) prefers `config.anthropicApiKey || env ANTHROPIC_API_KEY` over the incoming bearer. The resident service must therefore **never** set `ANTHROPIC_API_KEY` in its environment, or it would silently override every subscription request.
- **Gateway auth token lookup** (`src/gateway/auth.ts`): the primary `keyHeader` is tried first, then SDK-compatible fallbacks (`authorization`, `x-api-key`, …). Two consequences: (a) `AUTH_STATIC_API_KEY_BEARER_ONLY=false` is required for a bare custom-header key — the default expects `Bearer <key>` even in a custom header, and a format-invalid primary silently falls through to trying the OAuth bearer against the static keys (the confusing 401 mode hit mid-probe); (b) local-only consumers can keep authenticating the WP-5 way (`ANTHROPIC_AUTH_TOKEN=<gateway-key>` landing in `authorization`) on the same gateway instance, since `authorization` is a fallback candidate — both postures coexist in one config.
- **No `/v1/messages/count_tokens` route** — only `POST /v1/messages` is registered (`src/gateway/routes.ts:165`); count_tokens 404s through the gateway (works native against llama.cpp per WP-4).
- **No prefill keepalive** — ping machinery exists (`optimisticStreamKeepAliveFrame`, `src/gateway/streaming-conversion.ts`) but fires only for gateway-owned tool holds, and for Anthropic sources only after `message_start`. Nothing timer-based covers upstream prefill silence, confirming WP-5's measurement; the 300 s byte-silence watchdog gap is a real deficiency vs the protocol reference's generate-pings recommendation, and remains the second candidate engine extension.

## Corrections to earlier records (per `references/router-claude-gateway.md`)

- The trailing mid-array `role:"system"` message is a **documented Anthropic capability** (mid-conversation system messages), not a malformed request; Claude Code has documented fallback behavior when an upstream rejects it — which depends on the gateway forwarding upstream error bodies unmangled. Observed here: on upstream failure the gateway returned its own envelope (`401 All target providers failed.`), not the upstream body — an acceptance-test point for the resident plan.
- CCR's headless failure (WP-5) is an observed 3.0.22 packaging/runtime defect, not proof upstream abandoned headless; the depend-on-the-engine-directly decision stands on dependency-boundary grounds regardless.

## Resident-router plan inputs pinned by this probe

1. **Fix required**: suppress `x-api-key` emission when the Anthropic-leg credential came from the incoming bearer (or is `sk-ant-oat*`). Options for the plan: minimal local patch/overlay, upstream PR, or both. This is the only blocking change.
2. **Config posture proven**: providers = `claude-local` (`openai_chat_completions`, `apikey` set, → `:4811` — the WP-5 qwen38-fixing leg) + Anthropic passthrough provider (**no** `apikey`); gateway auth = static key on `x-gateway-key` with bearer-only off; subscription clients set `ANTHROPIC_BASE_URL` + `ANTHROPIC_CUSTOM_HEADERS`, **never** `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`; loopback bind.
3. **Open protocol gaps to carry as scope decisions**: prefill keepalive/ping generation; `?beta=true` and open-list header forwarding; upstream error-body passthrough; count_tokens. *(Status 2026-08-25: the first, third, and fourth shipped as patches 0002/0003/0004 via `plans/router-prefill-keepalive-and-protocol-gaps.md` — current behavior in `resident-router.md` "Protocol coverage"; only the `?beta=true`/open-list-header gap remains, accepted.)*
