# Mixed local/hosted routing — evaluation spike decision

> **Resident service shipped** (2026-08-25, `plans/resident-router-ai-gateway.md`): the successor plan this doc anticipated landed the router as `inference-engine-router` on `:4813`, with the subscription-Claude OAuth passthrough this doc recorded as unsupported now proven and patched in (`oauth-passthrough-probe.md` superseded that finding). Consumer guide: `resident-router.md`. The evidence below stands as the measurement record it was.

Plan: `plans/capability-surfacing-and-local-agent-endpoints.md` WP-5. All observations live-measured 2026-08-25 on this host against llama.cpp b10534 / llama-swap v243 (`:4811`) with Claude Code 2.1.243 headless (`claude -p`). Probed by response body throughout. All proxies were throwaway (installs under `/tmp/wp5-*`, loopback-only binds on 3456/4000, torn down at close); no service, port claim, compose change, or alias landed.

## Decision

**Local-first now; mixed routing is viable and recommended via the claude-code-router core engine (`@the-next-ai/ai-gateway`) when a hosted leg is wanted — but a resident router is only worth shipping once a hosted credential model the user accepts exists (see "Hosted leg" below).** Concretely:

1. **For local-only Claude Code use, the router already earns its keep** — it is the only tested arm that makes `qwen38-27b-iq3s` (the target coding alias) usable under Claude Code at all, fixing WP-4's mandatory blocker (details below). Plain per-profile env (arm c) remains fine for qwen36-class models but permanently excludes qwen38.
2. **LiteLLM is eliminated** by the plan's mandatory requirement: it cannot make qwen38 usable in any tested configuration.
3. **No hosted credential was tested.** The user's constraint (2026-08-25 hold): a hosted leg is only interesting if it can use *subscription* Claude (OAuth), not API-key billing; API-key-based mixing would go through OpenRouter and needs its test models picked first. Neither evaluated gateway supports Anthropic-subscription OAuth headless, so the hosted leg was skipped by user decision, and this doc pins the future OpenRouter test configuration instead.
4. If a resident router ships later, that is a successor plan with a `SERVICES.md` port claim; nothing here claims a port.

## The mandatory requirement (carried from WP-4) and how each arm fared

WP-4 established: Claude Code 2.1.243 unconditionally appends a trailing `role:"system"` message inside `messages`; the Qwen3.8 template 500s on it (`Jinja Exception: System message must be at the beginning`), making qwen38 unusable on the native path. An arm that does not fix this cannot win.

| arm | result | evidence |
| --- | --- | --- |
| (a) claude-code-router core, **`anthropic` (native passthrough) provider type** | **FAIL** — passes the mid-array system message through verbatim; upstream 500 unchanged | two-curl probe, HTTP 500, same Jinja exception |
| (a) claude-code-router core, **`openai_chat_completions` (conversion) provider type** | **PASS** — the Anthropic→OpenAI conversion **merges the trailing system message into the adjacent (preceding) user turn**, so no `system` role survives past index 0 | probe returns 200 with correct answer; content-preservation probe shows the system text appended to the user message; **full harness multi-tool task on qwen38 completed** (Write→Bash→Write, correct FizzBuzz RESULT.md, 2 m 55 s wall incl. model swap) |
| (b) LiteLLM `anthropic/` passthrough | **FAIL** — position preserved, upstream 500 | two-curl probe |
| (b) LiteLLM `openai/` conversion | **FAIL** — maps `role:"system"` in place, template still raises | two-curl probe |
| (b) LiteLLM with `litellm_settings.modify_params: true` | **FAIL** — no relocation on either route | re-probe after restart |
| (c) plain env (WP-4 baseline) | FAIL for qwen38 (that is the finding that created this requirement) | `docs/claude-code-local-profile.md` |

Nuance on the arm-(a) fix: the system text is folded into the *preceding user* message, i.e. it still lands after the user's prompt. Template compatibility is solved; the WP-4 small-model derail concern (nemotron-4b answering the agent-type listing instead of the task) is positional and therefore **not** solved — keep small models in the haiku slot only.

Harness-level confirmation of the LiteLLM failure: a real `claude -p` run against it surfaced `API Error: 500 … System message must be at the beginning`, attempted no tool calls, and burned 3 m 53 s in retries before giving up.

## What was actually run (samples per the execution brief)

Arm (a) = `@the-next-ai/ai-gateway` 1.0.18 (see "The CCR v3 pivot" for why this is the artifact under test), loopback `127.0.0.1:3456`, static-key auth (`AUTH_STATIC_API_KEYS`), file config (`GATEWAY_CONFIG_PATH`), node v24.15.0. Arm (b) = LiteLLM 1.98.0 (`litellm[proxy]` venv), loopback `127.0.0.1:4000`, master-key auth. Ports verified free (`ss -tln`) before bind.

- **Multi-tool harness parity task (qwen36-a3b)**: PASS through both arms (arm a: 2 m 58 s; arm b: 3 m 37 s, each incl. one model swap). Same task/fixture recipe as WP-4 (disposable fixture, deny-rule containment).
- **Mandatory qwen38 probe**: table above. Arm (a) full harness pass; arm (b) harness-level fail.
- **Added latency** (3 small non-streamed calls, warm qwen38, native re-baselined in the same window): native median ≈ 0.60–0.70 s; arm (a) ≈ 0.76 s; arm (b) ≈ 0.79 s. Both arms add ≈ 0.1–0.2 s/call — negligible against multi-second inference turns.
- **Streamed call / SSE fidelity**: both arms re-emit correct Anthropic SSE framing (`message_start → content_block_* → message_stop`; arm (a) verified even across its OpenAI conversion, thinking deltas intact). **Neither arm emits early bytes or keepalives during upstream prefill**: under a ~10k-token prompt (≈12.6 s prefill), first *body* byte arrived at prefill end on the native path and both proxy paths alike (a curl `time_starttransfer` of 0.25 s on the native path was headers-only — llama.cpp flushes response headers immediately, the gateways don't; first SSE event timestamped server-side at 12.65 s in all cases). **The WP-4 300 s silent-abort condition is unchanged by either router.**

## Primary axis 1 — credential injection/stripping

Both arms implement the model correctly: the client holds only a placeholder (`AUTH_STATIC_API_KEYS` value for arm a; `master_key` for arm b); the provider key lives server-side (config `apikey`, or `apiKeyEnv` for env-var injection — the right mechanism for a real hosted key: never at rest in config) and the client credential is never forwarded upstream.

Rejection behavior differs in polish: arm (a) returns clean 401s (`Invalid API key.` / `Missing auth token header`) on wrong/missing keys; arm (b) without a database returns a 400 `no_db_connection` on a wrong key and a 500 `internal_server_error` on a missing key — functional (nothing leaks) but noisy, because LiteLLM's virtual-key machinery assumes a Postgres it doesn't have here.

## Primary axis 2 — unified model list / discovery-filter id rewriting

Claude Code's gateway model discovery only surfaces ids containing `claude`/`anthropic` (WP-4). Both arms satisfy it with router-side rewriting only — `llama/config.yaml` untouched (alias contract M7):

- Arm (a): ids are `<providerName>/<model>`; naming the provider `claude-local` yields `claude-local/qwen36-a3b`, `claude-local/qwen38-27b-iq3s`, `claude-local/nemotron-4b` — all pass the filter. One `/v1/models` surface carries every provider's models, so local + hosted models would appear in a single merged list.
- Arm (b): `model_name` is fully arbitrary per entry (`claude-local-qwen38-27b` etc.) — cleaner id control, same merged-list property.

Task-type routing (incidental observation only, per plan): neither headless arm was exercised for it; CCR's `Router.default/background` config survives its v3 migration but routing was not evaluated (the wrapper that owns it doesn't run headless — below).

## The CCR v3 pivot — confirmed in the artifact, with a working escape hatch

The brief required assessing the v3 desktop-pivot risk from the installed artifact. It is real and worse than the README suggests:

- npm `@musistudio/claude-code-router` **3.0.22** self-describes as "Local Claude Code Router gateway with CLI and web management UI": sqlite config store (`better-sqlite3` — whose native bindings additionally **failed to build on install** and needed a manual `npm run build-release`), web SPA, bot-gateway (Slack/Telegram-class) machinery, media/MCP proxies. The legacy `~/.claude-code-router/config.json` is migrated into sqlite (and archived/deleted) on first run.
- **The actual proxy engine is not in the wrapper.** It resolves a `next-ai-gateway.js` entry from `dist/main/` (absent from the npm artifact) or Electron `resourcesPath` (CCR Desktop). Headless `ccr start --gateway` therefore fails: first with a same-port EADDRINUSE between web UI and gateway listeners, then — even with split ports and `CCR_GATEWAY_ENTRY` pointed at the dependency — the managed child exits 1 silently. The wrapper's own liveness probe then reports its own web UI as "a non-CCR service".
- **The escape hatch**: the engine ships as an ordinary dependency, `@the-next-ai/ai-gateway` (1.0.18) — a standalone Fastify gateway with file config, env-var auth/keys, Anthropic + OpenAI + Gemini protocol adapters, and no sqlite/desktop coupling. Run directly (`node …/bin/next-ai-gateway.js` with `GATEWAY_CONFIG_PATH`, `AUTH_*`, `HOST`, `PORT`), it is well-behaved and is what every arm-(a) result above measured. If a resident router ever ships here, **depend on `@the-next-ai/ai-gateway` directly and skip the CCR wrapper entirely.**

Working demo config (torn down; placeholder client key, local-only):

```json
{
  "host": "127.0.0.1",
  "port": 3456,
  "Providers": [
    {
      "name": "claude-local",
      "type": "openai_chat_completions",
      "apikey": "local-dummy",
      "baseurl": "http://127.0.0.1:4811/v1",
      "models": ["qwen36-a3b", "qwen38-27b-iq3s", "nemotron-4b"]
    }
  ]
}
```

launched with `AUTH_ENABLED=true AUTH_MODE=static_api_key AUTH_REQUIRED=true AUTH_STATIC_API_KEYS=<client-placeholder> HOST=127.0.0.1 PORT=3456`, consumed by a Claude Code fixture profile of `ANTHROPIC_BASE_URL=http://127.0.0.1:3456`, `ANTHROPIC_AUTH_TOKEN=<client-placeholder>`, `ANTHROPIC_MODEL=claude-local/qwen38-27b-iq3s`, `ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-local/nemotron-4b`.

## Hosted leg — skipped by user decision; future test pinned

User constraint (2026-08-25): hosted mixing is only interesting on *subscription* Claude (OAuth), not API-key billing; otherwise the hosted side would be OpenRouter, whose test models needed picking first.

- **Subscription-Claude support, checked in both artifacts**: `@the-next-ai/ai-gateway` supports upstream OAuth for **Codex only** (`providerPlugins[].codexOauth`, OpenAI refresh-token flow) — no Anthropic-subscription analogue. The CCR wrapper carries Claude-app OAuth/account machinery (`oauthAccount`, `claude-app-gateway`) but only in the desktop-coupled path this evaluation showed non-functional headless. LiteLLM is API-key based for Anthropic. **No tested gateway offers a clear headless subscription-Claude route today.**
- **Future OpenRouter leg, ready-to-run**: add to the config above a second provider `{"name": "anthropic-hosted", "type": "openai_chat_completions", "apiKeyEnv": "OPENROUTER_API_KEY", "baseurl": "https://openrouter.ai/api/v1", "models": [ … ]}` (key via env only). Recommended test models: `anthropic/claude-haiku-4.5` for the cheap wire-compat probe and `anthropic/claude-sonnet-4.5` as the actual hosted-orchestrator candidate — both keep `anthropic`/`claude` in the id, so the merged `/v1/models` list passes discovery unmodified. The three-call protocol from the WP-5 brief (1 merged list + 1 hosted + 1 local call through the same endpoint) applies as written.

## Operational surface (secondary axis)

- Arm (a) core engine: single node process, file config, env auth, no state directory needed; restart to reload config (file watch did not hot-reload the provider list). ~102 npm packages via the wrapper install; the engine could be installed standalone to shrink that.
- Arm (b): Python venv (~hundreds of packages incl. FastAPI/prisma), slower startup (~15 s), DB-assuming error paths without Postgres. Heavier for what it delivers here, and disqualified regardless.

## Errata noted during this spike

- Claude Code 2.1.243 now warns that `Write(//path/**)` **deny rules are not matched by file permission checks — only `Edit(path)` rules cover file-editing tools**. WP-4's fixture profile enforced correctly because it carried both forms; `docs/claude-code-local-profile.md` carries the correction note.
