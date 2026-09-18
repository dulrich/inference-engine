import { z } from "zod";

// Serving-control wire schemas (`plans/bench-dashboard-serving-control.md` WP-4) — the
// `POST /api/serving/visibility` request body, the on-disk `serving-request-v1` drop-box
// file that route writes, and the `serving-status-v1` file the host dispatcher (WP-5)
// publishes every pass and `GET /api/serving/status` reads back.
//
// The division of labour mirrors the control plane (`./control.ts`) exactly: the API only
// ever RECORDS a request file — `accepted: true` means *recorded*, never *executed* — and
// the host dispatcher is the sole writer of `router/visibility.json` and the sole authority
// over a request's outcome. The UI learns real outcomes by polling `GET /api/serving/status`.
//
// The API cannot pre-validate `cc` against the declaration: `router/visibility.json` is not
// mounted into the dashboard container and this plan adds no mount. Body pre-validation is
// therefore SHAPE ONLY (schema literal, non-empty `model_key`, closed consumer vocabulary);
// the `cc => gateway_id non-null` invariant is enforced in the UI (the chip is disabled from
// the status file's own `gateway_id`) and, authoritatively, by the dispatcher at ingest.

/** The two consumers of a served model's visibility, in canonical order. `cc` = the
 * Claude Code model picker (`router/model-picker-lineup.mjs`); `openwebui` = the Open WebUI
 * row's `is_active` (`scripts/sync-openwebui-capabilities.py`). Router reachability is not a
 * consumer: every served key is reachable over `:4811` regardless, and `[]` means
 * router-reachable-only. Append-only, like the alias contract itself. */
export const ServingConsumerSchema = z.enum(["cc", "openwebui"]);
export type ServingConsumer = z.infer<typeof ServingConsumerSchema>;

/** The canonical `consumers` order, single-sourced so the POST body builder, the fixtures
 * and `router/visibility.json`'s own ordering rule agree. `dashboard/scripts/
 * check-serving-recipes.ts` fails the declaration if a `consumers` array is out of this
 * order or carries a duplicate. */
export const SERVING_CONSUMER_ORDER: readonly ServingConsumer[] = ["cc", "openwebui"];

// ---- POST /api/serving/visibility body (API pre-validation only) ---------------------

/** `POST /api/serving/visibility` body. `consumers` is the FULL desired array for that
 * model, not a delta — the dispatcher writes it verbatim (canonically ordered,
 * deduplicated) into that model's declaration entry. camelCase: this is the browser's
 * shape, not the dispatcher's. */
export const ServingVisibilityRequestSchema = z.object({
  modelKey: z.string().min(1, "modelKey must be a non-empty llama/config.yaml model key"),
  consumers: z.array(ServingConsumerSchema),
});
export type ServingVisibilityRequest = z.infer<typeof ServingVisibilityRequestSchema>;

// ---- the on-disk `serving-request-v1` drop-box file ----------------------------------
//
// The API is the ONLY producer of these files and `llama/bakeoff/serving_dispatcher.py`
// (WP-5) is the only consumer, which is why the field names below are snake_case: this is
// the Python dispatcher's shape. The literal lives here, once, so the producer stamps the
// same string this schema validates; the committed fixtures under
// `apps/api/fixtures/serving/contract-v1/` are parsed by BOTH this schema and the Python
// validator, from the same bytes.

/** `serving_protocol.SCHEMA_REQUEST`. The dispatcher REQUIRES it — absent, wrong-typed or
 * unknown is a fail-closed `rejected` disposition, never a tolerated extra key. */
export const SERVING_REQUEST_SCHEMA_V1 = "serving-request-v1";

/** `serving_protocol.REQUEST_BASENAME_RE`, minus the `.json` suffix:
 * `srv-<UTCcompact yyyymmddTHHMMSSZ>-<16 hex>`. Deliberately a different prefix from the
 * bake-off plane's `req-` — the two planes share one drop-box directory and each ignores
 * the other's basenames at ingest. */
export const SERVING_REQUEST_ID_PATTERN = /^srv-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$/;

/** The one action this plane has. A separate enum (rather than a bare literal) so a second
 * serving action can be added without re-shaping the file. */
export const ServingActionSchema = z.enum(["set-visibility"]);
export type ServingAction = z.infer<typeof ServingActionSchema>;

/** The `serving-request-v1` file the API drops into `CONTROL_REQUESTS_DIR`. `model_key` is
 * the `llama/config.yaml` key, which is also the key of `router/visibility.json`'s `models`
 * map. `requested_at` is millisecond-resolution ISO-8601 UTC and is load-bearing: the
 * dispatcher orders same-model requests by `(requested_at, request_id)`, because the id's
 * whole-second stamp does not order two requests accepted in the same second. */
export const ServingRequestSchema = z.object({
  schema: z.literal(SERVING_REQUEST_SCHEMA_V1),
  request_id: z.string().regex(SERVING_REQUEST_ID_PATTERN, "request_id must match the serving request-id grammar"),
  action: ServingActionSchema,
  model_key: z.string().min(1),
  consumers: z.array(ServingConsumerSchema),
  requested_at: z.string().min(1),
});
export type ServingRequest = z.infer<typeof ServingRequestSchema>;

/** `POST /api/serving/visibility` success response — the same shape the control plane's
 * POSTs return (`ControlAcceptedResponseSchema`), restated here so this plane's consumers
 * don't have to import the bake-off plane's schema to read it. */
export const ServingAcceptedResponseSchema = z.object({
  requestId: z.string(),
  accepted: z.literal(true),
  message: z.string(),
});
export type ServingAcceptedResponse = z.infer<typeof ServingAcceptedResponseSchema>;

// ---- the on-disk `serving-status-v1` file, and its read-back route -------------------
//
// Written by the host dispatcher EVERY pass (including passes with no pending request), so
// the dashboard's chips are authoritative within a minute of install; read `:ro` by
// `GET /api/serving/status` from `CONTROL_HOST_DIR`. snake_case for the same reason as the
// request file: Python writes these bytes.

/** `serving_protocol.SCHEMA_STATUS`. */
export const SERVING_STATUS_SCHEMA_V1 = "serving-status-v1";

/** One model's declared visibility, verbatim from `router/visibility.json`'s `models` map
 * — the API re-publishes the declaration rather than re-deriving it, and this is the chips'
 * ONLY source of truth (never the `GET /api/serving` YAML parse, which carries no
 * visibility at all). `gateway_id === null` means the key has no Claude Code picker row and
 * `cc` is therefore invalid for it. */
export const ServingVisibilityEntrySchema = z.object({
  gateway_id: z.string().nullable(),
  consumers: z.array(ServingConsumerSchema),
});
export type ServingVisibilityEntry = z.infer<typeof ServingVisibilityEntrySchema>;

/** The whole declaration, keyed by `llama/config.yaml` model key. */
export const ServingVisibilityMapSchema = z.record(z.string(), ServingVisibilityEntrySchema);
export type ServingVisibilityMap = z.infer<typeof ServingVisibilityMapSchema>;

/** `router/visibility.json`'s own schema literal — single-sourced here, alongside
 * `SERVING_REQUEST_SCHEMA_V1`/`SERVING_STATUS_SCHEMA_V1` above, so the on-disk declaration the
 * host dispatcher writes, this package's own status-file republication, and the conformance
 * gate that checks it (`dashboard/scripts/check-serving-recipes.ts`) agree on one string.
 * Distinct plane from the two schemas above: those are the serving-control request/status
 * pair; this is the declaration FILE itself. */
export const SERVING_VISIBILITY_SCHEMA_V1 = "serving-visibility-v1";

/** The full `router/visibility.json` document — the schema literal plus its `models` map
 * (`ServingVisibilityMapSchema`, the same shape the status file republishes above). This is
 * the one shape the visibility conformance gate parses against, replacing that gate's own
 * hand-written duck-typed reader and its separately declared consumer vocabulary (review
 * finding F4 — boundary-type-contracts): a vocabulary/order change can no longer make the gate
 * and this package disagree while both typecheck, because there is exactly one declaration of
 * each. Per-entry canonical-order/duplicate-gateway-id and the cross-file max-tokens crosswalk
 * stay the gate's own business rules — they need alias-list and gateway-config context this
 * schema doesn't have. This strict form is for readers that want the whole document or nothing;
 * the GATE parses `ServingVisibilityEnvelopeSchema` below and applies `ServingVisibilityEntrySchema`
 * per entry, so one malformed entry cannot hide every other entry's diagnostics. */
export const ServingVisibilityDeclarationSchema = z.object({
  schema: z.literal(SERVING_VISIBILITY_SCHEMA_V1),
  models: ServingVisibilityMapSchema,
});
export type ServingVisibilityDeclaration = z.infer<typeof ServingVisibilityDeclarationSchema>;

/** The same envelope with its entries left UNVALIDATED — the shape a per-entry diagnostic tool
 * parses against. Derived from the declaration schema above (never a second hand-written
 * envelope), so the schema literal and the key set stay single-sourced: only the `models` value
 * type is relaxed. `dashboard/scripts/servingVisibilityConformance.ts` uses this, then applies
 * `ServingVisibilityEntrySchema` to each entry itself, because a conformance GATE must report
 * every non-conforming entry — validating the whole map in one parse would let one entry with
 * an off-vocabulary `consumers` token collapse every other entry's diagnostic into a single
 * whole-document failure, which is the one outcome that gate exists to prevent. Readers that
 * want the document or nothing (the API's status republication) use the strict schema above. */
export const ServingVisibilityEnvelopeSchema = ServingVisibilityDeclarationSchema.extend({
  models: z.record(z.string(), z.unknown()),
});
export type ServingVisibilityEnvelope = z.infer<typeof ServingVisibilityEnvelopeSchema>;

/** A request's terminal disposition. There are no in-flight states on this plane: the
 * dispatcher records a disposition and THEN unlinks the request file, so every id that
 * appears here is finished. `applied` = the declaration was rewritten; `rejected` = refused
 * at ingest and nothing was written; `failed` = the declaration WAS written but a generator
 * exited non-zero (the generators are the reconciliation loop — the next pass re-runs them
 * idempotently, so a `failed` request is self-healing, not lost work). */
export const ServingRequestStateSchema = z.enum(["applied", "rejected", "failed"]);
export type ServingRequestState = z.infer<typeof ServingRequestStateSchema>;

/** One entry of `recent[]` (bounded to 20, newest first) and the shape of `last_request`.
 * `reason` is `.default(null)` on READ so a writer that omits it on the happy path still
 * parses; canonical writers (and every committed fixture) emit it explicitly as `null`. */
export const ServingRequestRecordSchema = z.object({
  request_id: z.string(),
  state: ServingRequestStateSchema,
  reason: z.string().nullable().default(null),
  applied_at: z.string(),
});
export type ServingRequestRecord = z.infer<typeof ServingRequestRecordSchema>;

/** The `serving-status-v1` file itself. `last_request` is the newest entry of `recent[]`
 * (or `null` before the plane has ever dispositioned anything) — carried separately so a
 * reader that only wants "what happened last" does not have to know `recent[]`'s ordering
 * rule. */
export const ServingStatusFileSchema = z.object({
  schema: z.literal(SERVING_STATUS_SCHEMA_V1),
  generated_at: z.string().min(1),
  visibility: ServingVisibilityMapSchema,
  last_request: ServingRequestRecordSchema.nullable(),
  recent: z.array(ServingRequestRecordSchema),
});
export type ServingStatusFile = z.infer<typeof ServingStatusFileSchema>;

/** Why the status read-back has no payload — the router-status plane's own posture
 * (`packages/shared/src/routerStatus.ts`), never a 500. `disabled` = `CONTROL_HOST_DIR`
 * unset (absence is the feature-off signal); `unreadable` = the file is missing,
 * unreadable, not JSON, fails this schema, or carries an unparseable `generated_at`;
 * `empty` = the file exists but holds no bytes; `stale` = `generated_at` is older than
 * `SERVING_STATUS_STALE_MS`. Chips disable on `stale` exactly as on absent: a stopped cron
 * would otherwise leave them enabled over a map that is silently frozen. */
export const ServingStatusUnavailableReasonSchema = z.enum(["disabled", "unreadable", "empty", "stale"]);
export type ServingStatusUnavailableReason = z.infer<typeof ServingStatusUnavailableReasonSchema>;

/** 180 s — the queue control plane's own offline threshold (`llama/bakeoff/README.md`'s
 * Scheduling section), reused rather than re-chosen so the dashboard has ONE staleness
 * story. The dispatcher publishes minutely, so three missed passes is the verdict. */
export const SERVING_STATUS_STALE_MS = 180_000;

/** `GET /api/serving/status` — always 200, a discriminated union on `available`. The
 * `available: true` arm is the status file's own fields, snake_case and all: these bytes
 * are a cross-language contract (`apps/api/fixtures/serving/contract-v1/`), and re-casing
 * them onto the wire would create a second shape to keep in lockstep for no gain. */
export const ServingStatusResponseSchema = z.union([
  z.object({
    available: z.literal(false),
    reason: ServingStatusUnavailableReasonSchema,
  }),
  ServingStatusFileSchema.extend({ available: z.literal(true) }),
]);
export type ServingStatusResponse = z.infer<typeof ServingStatusResponseSchema>;
