# Router telemetry baseline — what the engine's native logging/metrics surface actually carries

Plan: `plans/router-observability-and-tool-search-conformance.md` WP-1. Measured 2026-09-12 (local; log timestamps UTC 2026-09-13 00:3x) on the resident router `:4813` after enabling the engine's native surface via `fleet-tools/compose.fleet.yml` (`GATEWAY_LOG_ENABLED=true`, `GATEWAY_LOG_LEVEL=info`, `GATEWAY_ACCESS_LOG=true`, `GATEWAY_METRICS_ENABLED=true`, plus a `json-file` logging driver at `max-size: 20m`, `max-file: 5`). Container recreated with `docker compose up -d inference-engine-router`; `router/smoke-router.sh` exit 0 afterwards (7 PASS, metered probes skipped). Every claim below is from that window's `docker logs` / `GET /metrics` / `docker stats`; nothing is quoted from engine docs.

## Verdict

The native surface is a real first tier — per-request **token usage** (input, output, cache read, cache write) joined to an access line by `reqId`, plus a `/metrics` health strip — but it is **not** the per-request record the plan wants: it carries **no session id as a field, no model id as a field, no TTFT, no prefill/decode timings, and no error envelope**, and at the only level that emits usage (`info`) it also dumps **up to 32,768 characters of every upstream request body** (system prompt + conversation) into the root-owned container log. WP-2's compact telemetry sink remains necessary; its field spec and sink decision are at the end.

## Per-request log chain (all legs)

One `POST /v1/messages` produces five pino JSON lines (`level: 30`), all joined by `reqId`. Measured chain for a streamed local-leg turn (`reqId 04b8cd7d…`, bodies stripped here):

| # | `msg` | fields | notes |
| --- | --- | --- | --- |
| 1 | `incoming request` | `req.{method,url,host,remoteAddress,remotePort}` | no model, no session, no headers |
| 2 | `Upstream request dispatched.` | `provider` (type), `providerName` (leg), `sourceAdapterKey`, `upstream.request.{url,headers,body}` | headers with `authorization`/`x-api-key` masked `***`; **`body` = the full upstream request JSON, truncated to a 32,768-char `preview` with `originalLength`** (e.g. `originalLength: 408608` for an orchestrator turn) |
| 3 | `Upstream response received.` | as 2 + `upstream.response.{status,headers,body}` | streaming body `"<streaming-response-body omitted>"`; non-streamed bodies are logged |
| 4 | `request completed` | `res.statusCode`, `responseTime` (ms, total wall) | `responseTime` is end-to-end, not TTFT |
| 5 | `Streaming usage billing computed.` / `Usage billing computed.` | `provider` (type only), `usage.{input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,total_tokens,cache_duration_seconds}`, `rates.*`, `cost.*` | rates/cost all `0` (no pricing configured); **no `providerName`, no model** on this line |

Leg-by-leg the chain is identical in shape; only `provider`/`providerName`/`upstream.request.url` differ:

| leg | `provider` | `providerName` | upstream url | usage example (this run) |
| --- | --- | --- | --- | --- |
| local (OpenAI conversion) | `openai` | `claude-local` | `http://inference-engine-llama:4806/v1/chat/completions` | in 17 / out 40 / cache_read 13 |
| hosted passthrough (this session's own turns) | `anthropic` | `anthropic` | `https://api.anthropic.com/v1/messages` | in 32 / out 1865 / cache_read 189,434 / cache_write 4,440 |
| codex sidecar | `anthropic` | `codex-b` | `http://host.docker.internal:4877/codex-b/v1/messages` | in 6,463 / out 5 |
| OpenRouter anthropic-shape | `anthropic` | `or-anthropic` | `https://openrouter.ai/api/v1/messages` | in 19 / out 40 |

Probe note for the two `type: "anthropic"` non-hosted legs: a bare curl **must send some `Authorization: Bearer …`** (`local-dummy` suffices) or the engine fails at `passthrough_build` with `ANTHROPIC_API_KEY is missing.` — same finding 3 as `openrouter-spike.md`; Claude Code's local-mode posture always sends one.

## Field gap table (what the plan asked for, field by field)

| field | native log | native `/metrics` | where it actually is |
| --- | --- | --- | --- |
| model id (requested / resolved) | **no field** | no | only inside the `upstream.request.body` preview (`"model":"…"`) |
| resolved provider / leg | yes — `provider`, `providerName` on lines 2–3 | yes — labels on every series | line 5 (usage) carries `provider` type only; join via `reqId` |
| `metadata.user_id` session id | **no field** (52/52 occurrences this window were inside the body preview, 0 as a field) | no | body preview only |
| request/response token counts | yes — line 5 `usage.*` | no (only `gateway_billing_events_total` counter) | — |
| TTFT | **no** (zero hits for `ttft`/`firstToken`/`time_to_first` in `/app/dist/index.js`) | no | nowhere |
| prefill / decode timings (llama `timings`) | **no** | no | lost at the conversion leg (line 3 omits streaming bodies; `timings` is in the SSE stream) |
| status | yes — line 4 `res.statusCode` | yes — `status_code` label | — |
| error envelope (`attempts[].stage/message`) | **no** — a failed request logs only `res.statusCode: 400`, no attempts | no | only in the HTTP response body to the client |
| wall time | line 4 `responseTime` (total) | `gateway_http_request_duration_{seconds,ms_*}` per route | — |

Other measured properties of the native log: level ≥ 40 in the window = 2 lines, both the engine's own `FastifyError: Reply was already sent … "/v1/models"` warning (pre-existing engine bug, harmless); no `x-claude-code-*` or `metadata` headers are logged (consistent with the gateway allowlist).

## `/metrics` (Prometheus text) — series that moved

`gateway_http_requests_total{method,route,status_class,status_code}`, `gateway_http_request_duration_seconds` (histogram) + `_ms_{sum,count,max}`, `gateway_stream_conversions_total{mode=passthrough|live,source_adapter,target_provider,target_provider_name}`, `gateway_billing_events_total`, `gateway_provider_{info,health_status,available,latency_ms}{provider,provider_name,type}`, `gateway_tool_executions_total`. Observed: `stream_conversions_total` passthrough/anthropic 6, passthrough/codex-a 11, live/claude-local 1; `provider_latency_ms` claude-local 93, anthropic 1859, codex-a 6; `duration_ms_max` `/v1/messages` 87,109 ms (an orchestrator turn). Aggregate only — **no session, no tokens, no TTFT**; a health strip, as the plan predicted.

## Cost of the native surface

- **Memory**: `docker stats` 72.78 MiB before the recreate → 75.74 MiB right after → 83.71 MiB after ~30 requests, of a 256 MiB cap. Headroom confirmed.
- **Log volume at `info`**: 271 lines / 3.04 MB in 15 min (avg 11.2 KB/line — the body previews), i.e. ~12 MB/h under one active orchestrator session. With `max-size: 20m × max-file: 5` that is roughly **8 hours of retention** before rotation discards it. The json-file is root-owned under `/var/lib/docker/containers/…` (uid 1001 cannot read it without `docker logs`).
- **Privacy**: at `info`, every upstream request body — system prompt, `CLAUDE.md` contents, conversation, tool results — is logged up to 32 KB per request. There is **no knob** to keep usage lines and drop bodies: `GATEWAY_LOG_LEVEL=warn` silences both (usage is emitted with `.info`), the body-log skip (`skipResponseBodyLog`) is internal, and `GATEWAY_BODY_LIMIT_BYTES` is the request-size limit, not a log limit. The full `GATEWAY_*` knob list was extracted from the built engine (~120 keys: auth, CORS, idempotency, OAuth, policy allow/deny, scheduling/cache-affinity, upstream retry/circuit-breaker, transparent tool execution, WS); none is a log-redaction knob.

## The engine's other native sink: raw trace capture

The built engine also has a **raw-trace spool**: `RAW_TRACE_ENABLED` (default off), `RAW_TRACE_MODE` (default `body_full`; `disabled` also honoured), `RAW_TRACE_SPOOL_DIR` (default `.raw-trace-spool`), `RAW_TRACE_MAX_PART_BYTES` (50 MiB parts), plus an uploader (`RAW_TRACE_SYNC_*`: URL/stdio transport, API key header, concurrency, retry). It is a **full-body capture designed for upload to a vendor sink**, not a compact per-request record — the wrong shape for the dashboard and worse than the info log on privacy. Not enabled; recorded so WP-2 does not rediscover it.

## Sink decision for WP-2

- **Host path, bind-mounted**: `~/.local/state/inference-router/logs/` → container `/var/log/inference-router/` (rw), one `router-telemetry-YYYY-MM-DD.jsonl` per UTC day, following `codex-sidecar/logs/` (0700 dir, daily files, retention prune) and `inference-bakeoff/control/` (host-created before `up`). Not a named volume — the reader is a uid-1001 host process (dashboard/report) and the stale-mount quirk is the same either way.
- **Ownership**: the container runs as **root** (`docker exec … id` → `uid=0`; the image has no `USER` line, entrypoint `node bin/next-ai-gateway.js`). Decision: add `user: "1001:1001"` to the router service — the process needs nothing root (unprivileged port 4813, read-only config bind, no capabilities), and it makes the sink files dulrich-owned without a setgid dance. WP-2 verifies the container still starts and `smoke-router.sh` is green under that uid; fallback if the image resists is the `control/requests` pattern (host dir `2775` setgid + writer `0664`). Create the dir with owner/mode **before** `up`; write via append + fsync per line; the file must exist non-empty after the first request or the mount is stale.
- **Record spec** (one JSON line per completed `/v1/messages` or `/v1/chat/completions`): `ts`, `reqId`, `route`, `leg` (= `providerName`), `providerType`, `modelRequested`, `modelResolved` (from `message_start` on the response), `sessionId` + `isSubagent` (parsed from `metadata.user_id` and the billing line), `stream`, `status`, `ttftMs` (first upstream byte → first client byte after `message_start`), `wallMs`, `usage.{input,output,cacheRead,cacheWrite}`, `timings` (llama `prompt_ms`/`predicted_ms`/`predicted_per_second` when the upstream SSE carries them — the conversion leg must read them before dropping the chunk), `stopReason`, `error` (`stage` + `message` from the attempts envelope when non-2xx). No bodies, no prompts, no headers.
- **Log level posture after WP-2 lands**: `GATEWAY_LOG_LEVEL=warn` (drop the body-logging info tier; the sink supersedes it) with `GATEWAY_ACCESS_LOG` left on for the `req`/`res` lines and `/metrics` kept for the health strip. Until then the posture is the user's call (see FEEDBACK/hold).

## Replace-vs-integrate verdict (required before any semantic patch `0006`)

**Continue vendoring on the `782cd51` pin; `0006` is a telemetry hook, not a rewrite.** Evidence: (1) the fields the sink needs already exist at one site in the engine — the `Streaming usage billing computed.` / `Usage billing computed.` emit point has `reqId`, `usage`, provider, and the resolved target in scope; adding session id, model, TTFT and the llama `timings` there is a bounded integration, not a semantic change to `handler.ts`/`streaming-conversion.ts` routing; (2) the four shipped semantic patches (`0001`–`0004`) and `0005` all applied cleanly and are live-verified today — no evidence the pin is rotting; (3) a rebase to a newer upstream was **not** measured here (no network fetch in a live-service spike) and would carry the whole series forward for no field we need; (4) replacing the engine would forfeit the OAuth passthrough (`0001`), keepalive (`0002`), error passthrough (`0003`) and `count_tokens` (`0004`) behaviours the fleet depends on. Proposing the hook upstream is optional and cheap once the patch exists. WP-2 is therefore sized as written in the plan.

### Second entry — 2026-09-12, before semantic patch `0007` (effort mapping, `plans/router-effort-mapping-claude-local.md` WP-1)

**Continue vendoring on the `782cd51` pin; `0007` is a ~60-line semantic patch at the engine's own extension points.** Evidence (spike record `fleet-tools/runs/dispatch/router-effort-mapping-design-spike-record.md`): (1) the hook file `src/adapters/builtins/target/shared.ts` (`rewriteOpenAIChatCompatibleRequest`, `:22-53`) and the `openai-chat-compat/` registry are byte-identical between the pin and upstream `origin/HEAD` (`3fd93f8`, 2026-09-08, 18 commits ahead) — a rebase changes nothing at the seam; (2) upstream drift in the two config files the patch touches is additive (`config.ts` +905, `types.ts` +396) and upstream still carries no `chat_template_kwargs` or per-model effort→kwargs mapping, so there is nothing to adopt by upgrading; (3) `0001`–`0006` all apply cleanly to the pin today; (4) replacing the engine forfeits `0001`–`0004` and `0006`. The patch adds one post-branch step in `rewriteOpenAIChatCompatibleRequest`, a `ProviderConfig.effortKwargs` field with its parser, and nothing in `handler.ts`. Upstreaming the step is optional. Per `router/Dockerfile`'s header clause, the next semantic patch after `0007`, or the first nontrivial pin-upgrade rebase, re-triggers this spike.
