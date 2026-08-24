import { z } from "zod";

// Control-plane wire schemas (`plans/bakeoff-queue-control-plane.md` WP-4) — the
// `GET /api/queue` `control` section (a read-only projection of the host dispatcher's
// `control/host/status.json`, `control-status-v1`, snake_case on disk -> camelCase on
// the wire) plus the `POST /api/control/*` request/response shapes. The host dispatcher
// (WP-3, not yet built) is the sole writer of `status.json` and the sole authority over
// requests: the API's POST routes only ever RECORD a request file into the drop-box —
// `accepted: true` means *recorded*, never *executed*; the UI learns real outcomes by
// polling `GET /api/queue`.

/** The pinned wire-state enum for one request's projected lifecycle state (cycle-1 E1),
 * precedence disposition -> live -> claim -> pending (`busy`/`launched` are internal
 * dispatcher stages, never wire states). */
export const ControlRequestStateSchema = z.enum([
  "pending",
  "claimed",
  "running",
  "done",
  "rejected",
  "canceled",
  "launch-failed",
]);
export type ControlRequestState = z.infer<typeof ControlRequestStateSchema>;

/** The pinned `lastRun.disposition` enum (cycle-2 E2); the exact
 * `run_exit`/`verify_exit`/queue-state -> disposition mapping is owned by the host
 * dispatcher (WP-3), not this shared schema. */
export const ControlLastRunDispositionSchema = z.enum(["clean", "needs-attention", "crashed"]);
export type ControlLastRunDisposition = z.infer<typeof ControlLastRunDispositionSchema>;

/** One control action — both the on-disk `control-request-v1` `action` field and the
 * wire request digest's `action`. */
export const ControlActionSchema = z.enum(["arm", "disarm", "start", "stop", "cancel"]);
export type ControlAction = z.infer<typeof ControlActionSchema>;

export const ControlLiveSchema = z.object({
  requestId: z.string(),
  queueStem: z.string(),
  phase: z.string(),
  startedAt: z.string(),
});
export type ControlLive = z.infer<typeof ControlLiveSchema>;

export const ControlLastRunSchema = z.object({
  requestId: z.string(),
  endedAt: z.string(),
  runExit: z.number().int().nullable(),
  verifyExit: z.number().int().nullable(),
  disposition: ControlLastRunDispositionSchema,
});
export type ControlLastRun = z.infer<typeof ControlLastRunSchema>;

export const ControlRequestDigestSchema = z.object({
  requestId: z.string(),
  action: ControlActionSchema,
  state: ControlRequestStateSchema,
  reason: z.string().nullable(),
  requestedAt: z.string(),
});
export type ControlRequestDigest = z.infer<typeof ControlRequestDigestSchema>;

/** `GET /api/queue`'s `control` section (cycle-1 M4: this must be threaded into
 * `QueueListResponseSchema` itself — a closed zod object — or the web client silently
 * strips the field). `null` whenever `CONTROL_*` env is unset, the host mount is
 * missing, `status.json` is missing/unparseable, or it fails this schema — honest null,
 * never a 500 (mirrors the queue route's own malformed-file philosophy). */
export const ControlSectionSchema = z.object({
  statusGeneratedAt: z.string(),
  stale: z.boolean(),
  armed: z.boolean(),
  live: ControlLiveSchema.nullable(),
  lastRun: ControlLastRunSchema.nullable(),
  anomalies: z.array(z.string()),
  requests: z.array(ControlRequestDigestSchema),
});
export type ControlSection = z.infer<typeof ControlSectionSchema>;

// ---- POST /api/control/* request bodies (API pre-validation only — UX guardrails; the
// host dispatcher re-validates fail-closed and is the sole authority) -----------------

/** `POST /api/control/start` body. Stem grammar + minutes bounds mirror the dispatcher's
 * own ingest validation (plan "Dispatcher" section (1)) — kept in lockstep by hand, not
 * by import, since the two live in different languages/processes. */
export const ControlStartRequestSchema = z.object({
  queueStem: z.string().regex(/^[a-z0-9][a-z0-9-]*$/, "queueStem must match ^[a-z0-9][a-z0-9-]*$"),
  minutes: z.number().int().min(15).max(480),
});
export type ControlStartRequest = z.infer<typeof ControlStartRequestSchema>;

/** `POST /api/control/cancel` body. `targetRequestId` grammar is the pinned request-id
 * grammar (plan "Filesystem contract": `req-<UTCcompact>-<16 hex>`). */
export const ControlCancelRequestSchema = z.object({
  targetRequestId: z
    .string()
    .regex(/^req-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$/, "targetRequestId must match the request-id grammar"),
});
export type ControlCancelRequest = z.infer<typeof ControlCancelRequestSchema>;

/** Every `POST /api/control/*` success response. */
export const ControlAcceptedResponseSchema = z.object({
  requestId: z.string(),
  accepted: z.literal(true),
  message: z.string().optional(),
});
export type ControlAcceptedResponse = z.infer<typeof ControlAcceptedResponseSchema>;
