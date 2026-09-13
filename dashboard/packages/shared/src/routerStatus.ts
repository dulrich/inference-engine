import { z } from "zod";

// Router status wire schemas (`plans/router-observability-and-tool-search-conformance.md`
// WP-3) — the read-only data plane behind `GET /api/router/{telemetry,slots,sidecar}`.
//
// Three independent feeds, one shape discipline: each response is a discriminated union on
// `available`. `available: false` carries a `reason` and NOTHING else — absence of the
// feature's env var (`disabled`), a configured-but-unreadable/stale-mounted directory
// (`unreadable`), or a readable directory holding no usable record yet (`empty`). None of
// those is ever a 500 (`config.ts`'s `controlRequestsDir` convention: absence is the
// feature-off signal).
//
// Provenance of every field below is a FILE on disk, never a live call — the dashboard API
// makes zero outbound HTTP (`apps/api/src/serving/llamaConfig.ts:18-33`, unchanged by this
// WP). The router's own jsonl sink (`router/patches/0006-request-telemetry-jsonl.patch`)
// feeds `telemetry`; a host-side unit (`dashboard/scripts/router-slots-poller.mjs`) is the
// only process that ever talks to llama-swap and writes `slots-YYYY-MM-DD.jsonl` beside the
// sink; the codex sidecar's own jsonl feeds `sidecar`.

/** Why a feed has no payload. `disabled` = env var unset; `unreadable` = configured dir
 * missing/unreadable (the fleet's stale-mount quirk yields an empty dir, not an error);
 * `empty` = dir readable but holds no parseable record for the feed. */
export const RouterUnavailableReasonSchema = z.enum(["disabled", "unreadable", "empty"]);
export type RouterUnavailableReason = z.infer<typeof RouterUnavailableReasonSchema>;

const RouterUnavailableSchema = z.object({
  available: z.literal(false),
  reason: RouterUnavailableReasonSchema,
});
export type RouterUnavailable = z.infer<typeof RouterUnavailableSchema>;

// ---- telemetry ----------------------------------------------------------------------

/** llama-server's verbatim `timings` block, passed through by the sink on the legs that
 * supply it (numeric members only, incl. the MTP counters `draft_n`/`draft_n_accepted`). */
export const RouterUpstreamTimingsSchema = z.record(z.string(), z.number());
export type RouterUpstreamTimings = z.infer<typeof RouterUpstreamTimingsSchema>;

/** One terminal `kind: "request"` line of the sink. Field names and nullability are the
 * sink's, verified against 558 live records in a 1 MiB tail (2026-09-13): every one of the
 * 25 keys is present on every record. The six trailing keys are nevertheless `.optional()`
 * so a sink revision that drops one degrades to a thinner row rather than to a silently
 * skipped record; `.passthrough()` keeps any key the sink ADDS, because the record shape
 * evolves under `router/`, not here. */
export const RouterRequestRecordSchema = z
  .object({
    kind: z.literal("request"),
    ts: z.string(),
    reqId: z.string(),
    route: z.string(),
    method: z.string(),
    leg: z.string().nullable(),
    modelResolved: z.string().nullable(),
    sessionId: z.string().nullable(),
    isSubagent: z.boolean(),
    stream: z.boolean(),
    status: z.number().nullable(),
    responseTokens: z.number().nullable(),
    reasoningTokens: z.number().nullable(),
    ttftMs: z.number().nullable(),
    wallMs: z.number().nullable(),
    error: z.unknown().nullable(),
    modelRequested: z.string().nullable().optional(),
    providerType: z.string().nullable().optional(),
    sourceAdapterKey: z.string().nullable().optional(),
    upstreamUrl: z.string().nullable().optional(),
    upstreamFirstByteMs: z.number().nullable().optional(),
    attemptSequence: z.number().nullable().optional(),
    stopReason: z.string().nullable().optional(),
    upstreamTimings: RouterUpstreamTimingsSchema.nullable().optional(),
    usage: z
      .object({
        input: z.number().nullable(),
        output: z.number().nullable(),
        cacheRead: z.number().nullable(),
        cacheWrite: z.number().nullable(),
      })
      .passthrough()
      .nullable()
      .optional(),
  })
  .passthrough();
export type RouterRequestRecord = z.infer<typeof RouterRequestRecordSchema>;

/** One throttled `kind: "progress"` line (cadence 1000 ms per in-flight request). Never
 * returned on the wire directly — it is the raw material `inFlight` is derived from. */
export const RouterProgressRecordSchema = z
  .object({
    kind: z.literal("progress"),
    ts: z.string(),
    reqId: z.string(),
    sessionId: z.string().nullable(),
    leg: z.string().nullable(),
    modelResolved: z.string().nullable(),
    phase: z.enum(["prefill", "decode"]),
    responseTokens: z.number(),
    reasoningTokens: z.number(),
    elapsedMs: z.number(),
  })
  .passthrough();
export type RouterProgressRecord = z.infer<typeof RouterProgressRecordSchema>;

/** A generation still running at read time: a `reqId` with progress lines but no terminal
 * `request` line in the same window. `stale` is a READER verdict, never a sink field — a
 * progress-only `reqId` whose `lastSeenTs` is older than the feed's `staleMs` is dropped
 * here rather than reported (the plan's "abandoned stream ages out" rule), so everything
 * in this array is live as of `generatedAt`. `tokPerSec` is the instantaneous rate across
 * the last TWO progress samples (`Δ(response+reasoning) / ΔelapsedMs * 1000`), `null` with
 * fewer than two samples or a non-advancing clock. */
export const RouterInFlightSchema = z.object({
  reqId: z.string(),
  leg: z.string().nullable(),
  modelResolved: z.string().nullable(),
  sessionId: z.string().nullable(),
  phase: z.enum(["prefill", "decode"]),
  responseTokens: z.number(),
  reasoningTokens: z.number(),
  elapsedMs: z.number(),
  lastSeenTs: z.string(),
  tokPerSec: z.number().nullable(),
});
export type RouterInFlight = z.infer<typeof RouterInFlightSchema>;

/** Per-leg rollup over the `recent` set of the SAME response (never over the whole file).
 * `errors` counts `status >= 400 || error != null`; `p50TtftMs` is the median over the
 * leg's non-null `ttftMs` values, `null` when the leg produced none. `leg: null` is the
 * unattributed bucket (a request that failed before target resolution). */
export const RouterLegSummarySchema = z.object({
  leg: z.string().nullable(),
  requests: z.number().int().nonnegative(),
  errors: z.number().int().nonnegative(),
  p50TtftMs: z.number().nullable(),
});
export type RouterLegSummary = z.infer<typeof RouterLegSummarySchema>;

/** `GET /api/router/telemetry`. `recent` is in FILE order (oldest → newest, the plan's
 * "filename order then line order" tail order), capped to the newest `maxRecent` records;
 * `legs` is computed over exactly that capped set, so the response is self-consistent.
 * `file` is the basename the tail came from, `windowBytes` the byte window it was read
 * through — both are provenance for the view, not settings. */
export const RouterTelemetryResponseSchema = z.discriminatedUnion("available", [
  z.object({
    available: z.literal(true),
    generatedAt: z.string(),
    windowBytes: z.number().int().positive(),
    file: z.string(),
    recent: z.array(RouterRequestRecordSchema),
    inFlight: z.array(RouterInFlightSchema),
    legs: z.array(RouterLegSummarySchema),
  }),
  RouterUnavailableSchema,
]);
export type RouterTelemetryResponse = z.infer<typeof RouterTelemetryResponseSchema>;

// ---- slots --------------------------------------------------------------------------

/** One llama-server `/slots` entry as this API projects it. `promptTokens` /
 * `promptTokensCache` are nullable because the current llama-server build returns only
 * `{id, n_ctx, speculative, is_processing}` for an IDLE slot (measured 2026-09-13) — the
 * prompt/cache counters appear on a processing slot. `cacheHitRatio` is therefore null
 * unless both are present and `promptTokens > 0`. */
export const RouterSlotSchema = z.object({
  id: z.number().nullable(),
  nCtx: z.number().nullable(),
  isProcessing: z.boolean().nullable(),
  promptTokens: z.number().nullable(),
  promptTokensCache: z.number().nullable(),
  cacheHitRatio: z.number().nullable(),
});
export type RouterSlot = z.infer<typeof RouterSlotSchema>;

/** The serving recipe parsed out of llama-swap's per-model `cmd` string. Every member is
 * nullable: an absent flag is `null`, never a guessed default (`serving/llamaConfig.ts`'s
 * posture). `mtp` is `--spec-type` OR `-md` present (`docs/model-capabilities.md:27`), and
 * is `null` only when there is no `cmd` to parse at all. */
export const RouterSlotRecipeSchema = z.object({
  ctk: z.string().nullable(),
  ctv: z.string().nullable(),
  fa: z.string().nullable(),
  reasoningBudget: z.number().nullable(),
  mtp: z.boolean().nullable(),
  moeCpuLayers: z.number().nullable(),
});
export type RouterSlotRecipe = z.infer<typeof RouterSlotRecipeSchema>;

/** One model as of the newest poller sample. `slots: []` with a non-`ready` `state` means
 * the poller deliberately did NOT proxy it — querying `/upstream/<model>/slots` for a
 * non-resident model SPAWNS it (the plan's load-bearing hazard), so only `ready` models
 * are ever polled. `error` is a per-model fetch failure the poller recorded; it never
 * aborts a tick. */
export const RouterSlotModelSchema = z.object({
  model: z.string(),
  state: z.string().nullable(),
  ttl: z.number().nullable(),
  nCtx: z.number().nullable(),
  recipe: RouterSlotRecipeSchema,
  slots: z.array(RouterSlotSchema),
  error: z.string().nullable(),
});
export type RouterSlotModel = z.infer<typeof RouterSlotModelSchema>;

/** `GET /api/router/slots`, built from the NEWEST `kind: "slots"` record in the poller's
 * jsonl. `stale` is a reader verdict (`ageMs > staleMs`, default 30 s) — the poller writes
 * `sampledAt`, never `stale`, exactly like the control plane's `status.json`. */
export const RouterSlotsResponseSchema = z.discriminatedUnion("available", [
  z.object({
    available: z.literal(true),
    sampledAt: z.string(),
    ageMs: z.number(),
    stale: z.boolean(),
    models: z.array(RouterSlotModelSchema),
  }),
  RouterUnavailableSchema,
]);
export type RouterSlotsResponse = z.infer<typeof RouterSlotsResponseSchema>;

/** The on-disk record `dashboard/scripts/router-slots-poller.mjs` appends and
 * `apps/api/src/router/slotsFeed.ts` reads — ONE definition shared by the writer and the
 * reader, so the host unit and the container cannot drift apart. `models[].slots` is the
 * RAW llama-server `/slots` array (snake_case, passed through verbatim) or `null` when the
 * model was not polled; the camelCase projection happens reader-side. Top-level `error`
 * records a `/running` failure — a tick against an absent llama-swap writes a record with
 * `models: []` and an error string, never a crash. */
export const RouterSlotsRecordSchema = z.object({
  kind: z.literal("slots"),
  ts: z.string(),
  sampledAt: z.string(),
  error: z.string().nullable(),
  models: z.array(
    z.object({
      model: z.string(),
      state: z.string().nullable(),
      ttl: z.number().nullable(),
      cmd: z.string().nullable(),
      slots: z.array(z.record(z.string(), z.unknown())).nullable(),
      error: z.string().nullable(),
    }),
  ),
});
export type RouterSlotsRecord = z.infer<typeof RouterSlotsRecordSchema>;

// ---- sidecar ------------------------------------------------------------------------

/** One line of `~/.local/state/codex-sidecar/logs/codex-sidecar-YYYY-MM-DD.jsonl`. The
 * kind set is OPEN by design (13 kinds observed live on 2026-09-13; the sidecar adds more
 * without asking this schema), so `kind` is a bare string and everything past the three
 * required keys is optional + `.passthrough()`. */
export const RouterSidecarRecordSchema = z
  .object({
    ts: z.string(),
    kind: z.string(),
    persona: z.string(),
    threadId: z.string().nullable().optional(),
    model: z.string().nullable().optional(),
    status: z.number().nullable().optional(),
    message: z.string().nullable().optional(),
    text: z.string().nullable().optional(),
  })
  .passthrough();
export type RouterSidecarRecord = z.infer<typeof RouterSidecarRecordSchema>;

/** Latest state per `persona` in the window. `threadId`/`model` come from the most recent
 * record that carries them (`config-verified` and `http` both do); `lastKind`/`lastSeenTs`
 * come from the most recent record of ANY kind. */
export const RouterSidecarPersonaSchema = z.object({
  persona: z.string(),
  threadId: z.string().nullable(),
  model: z.string().nullable(),
  lastSeenTs: z.string(),
  lastKind: z.string(),
});
export type RouterSidecarPersona = z.infer<typeof RouterSidecarPersonaSchema>;

/** An error-ish line: `http-error`, `child-stderr` or `orphan-tool-result`. `message`
 * falls back to the record's `text` (that is where `child-stderr` puts it) and is null for
 * `orphan-tool-result`, which carries neither. */
export const RouterSidecarErrorSchema = z.object({
  ts: z.string(),
  persona: z.string(),
  kind: z.string(),
  status: z.number().nullable(),
  message: z.string().nullable(),
});
export type RouterSidecarError = z.infer<typeof RouterSidecarErrorSchema>;

/** `GET /api/router/sidecar` — a summary of the sidecar jsonl tail, never the raw lines.
 * `byKind` is an open map (count whatever appears); `recentErrors` is the newest ≤ 20. */
export const RouterSidecarResponseSchema = z.discriminatedUnion("available", [
  z.object({
    available: z.literal(true),
    generatedAt: z.string(),
    file: z.string(),
    byKind: z.record(z.string(), z.number().int().nonnegative()),
    personas: z.array(RouterSidecarPersonaSchema),
    recentErrors: z.array(RouterSidecarErrorSchema),
  }),
  RouterUnavailableSchema,
]);
export type RouterSidecarResponse = z.infer<typeof RouterSidecarResponseSchema>;
