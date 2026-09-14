import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import type { ServingAcceptedResponse, ServingRequest, ServingStatusResponse } from "@bench-dashboard/shared";
import {
  SERVING_REQUEST_SCHEMA_V1,
  SERVING_STATUS_STALE_MS,
  ServingStatusFileSchema,
  ServingVisibilityRequestSchema,
} from "@bench-dashboard/shared";
import type { Hono } from "hono";
import { config } from "../config.ts";
import { writeControlRequestExclusive } from "./control.ts";

// `POST /api/serving/visibility` + `GET /api/serving/status` (WP-4,
// `plans/bench-dashboard-serving-control.md`) — the dashboard's first config-MUTATING path,
// and it mutates nothing directly: the POST writes ONE `serving-request-v1` file into the rw
// drop-box (`config.controlRequestsDir`) with the control plane's own collision-safe
// exclusive-create mechanics (`writeControlRequestExclusive`, reused rather than duplicated),
// and the host dispatcher (WP-5) is the sole writer of `router/visibility.json`.
// `accepted: true` means *recorded*, never *executed*.
//
// `GET /api/serving` is untouched by this file and stays a read-only parse of the
// bind-mounted YAML; this API still makes zero outbound HTTP and never writes either
// `llama/config.yaml` or `router/visibility.json`.
//
// Route logic is plain exported functions taking the directory (and, for the status read,
// `now`) as arguments — the `recordStartAction(requestsDir, …)` seam pattern, for the same
// reason: `config` is a singleton computed once per `bun test` process, and staleness is not
// testable against a wall clock.

/** The serving plane's request-id grammar: `srv-<UTCcompact yyyymmddTHHMMSSZ>-<16 hex from
 * randomUUID>`. A different prefix from the bake-off plane's `req-` on purpose — both
 * planes drop into one directory and each ignores the other's basenames at ingest. Exported
 * so tests can exercise the id-collision retry path deterministically. */
export function generateServingRequestId(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const compact =
    `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}T` +
    `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}Z`;
  const hex16 = randomUUID().replace(/-/g, "").slice(0, 16);
  return `srv-${compact}-${hex16}`;
}

/** The message every accepted visibility request carries: the write is recorded, and the
 * effect lands when the minutely host dispatcher next runs. */
export const SERVING_REQUEST_ACCEPTED_MESSAGE =
  "Visibility change recorded; the effect lands on the next dispatcher pass (about a minute).";

type ServingActionStatus = 200 | 400 | 503;

export interface ServingActionResult {
  status: ServingActionStatus;
  body: ServingAcceptedResponse | { error: string };
}

/** Same message shape as the control plane's own unconfigured result — absence of
 * `CONTROL_REQUESTS_DIR` is the pinned "control features off" signal, and the two planes
 * share the drop-box, so they share the diagnosis. */
function unconfiguredResult(): ServingActionResult {
  return {
    status: 503,
    body: { error: "control plane not configured: CONTROL_REQUESTS_DIR is unset" },
  };
}

/** `POST /api/serving/visibility` — `{modelKey, consumers}`, where `consumers` is the FULL
 * desired array for that model, not a delta.
 *
 * Pre-validation is SHAPE ONLY (non-empty `modelKey`, every consumer inside the closed
 * vocabulary), and deliberately so: `router/visibility.json` is not mounted into this
 * container, so the route cannot check `cc` against the declaration's `gateway_id` and does
 * not pretend to. That invariant is enforced by the UI (the chip is disabled from the status
 * file's own `gateway_id`) and, authoritatively, by the dispatcher, which rejects such a
 * request at ingest and never writes the declaration. A hand-crafted `curl` can still record
 * an invalid request; it dies at ingest. */
export function recordServingVisibilityAction(
  requestsDir: string | undefined,
  body: unknown,
  idFactory: () => string = generateServingRequestId,
): ServingActionResult {
  if (!requestsDir) return unconfiguredResult();
  const parsed = ServingVisibilityRequestSchema.safeParse(body);
  if (!parsed.success) {
    return { status: 400, body: { error: parsed.error.issues.map((issue) => issue.message).join("; ") } };
  }
  const { modelKey, consumers } = parsed.data;
  const requestId = writeControlRequestExclusive<ServingRequest>(
    requestsDir,
    (id, requestedAt) => ({
      schema: SERVING_REQUEST_SCHEMA_V1,
      request_id: id,
      action: "set-visibility",
      model_key: modelKey,
      consumers,
      requested_at: requestedAt,
    }),
    idFactory,
  );
  return { status: 200, body: { requestId, accepted: true, message: SERVING_REQUEST_ACCEPTED_MESSAGE } };
}

/** The status file's basename inside `CONTROL_HOST_DIR` — the host dispatcher's publish
 * target, beside the bake-off plane's own `status.json`. */
export const SERVING_STATUS_BASENAME = "serving-status.json";

/** `GET /api/serving/status` — ALWAYS 200, never a 500 (the router-status plane's posture):
 * an unset env var, a missing/unreadable/unparseable file, an empty one, or a `generated_at`
 * older than `SERVING_STATUS_STALE_MS` each answer a typed `{available: false, reason}`, and
 * the UI renders disabled chips with that reason rather than guessing a visibility state.
 *
 * `now` is injected so the 180 s staleness boundary is testable; the route passes the real
 * clock. A `generated_at` that does not parse is `unreadable`, not `stale` — a malformed
 * timestamp is a protocol error, and fail-closed is the same verdict either way. */
export function readServingStatus(hostDir: string | undefined, now: Date = new Date()): ServingStatusResponse {
  if (!hostDir) return { available: false, reason: "disabled" };
  let raw: string;
  try {
    raw = readFileSync(join(hostDir, SERVING_STATUS_BASENAME), "utf8");
  } catch {
    return { available: false, reason: "unreadable" };
  }
  if (raw.trim() === "") return { available: false, reason: "empty" };
  let json: unknown;
  try {
    json = JSON.parse(raw) as unknown;
  } catch {
    return { available: false, reason: "unreadable" };
  }
  const parsed = ServingStatusFileSchema.safeParse(json);
  if (!parsed.success) return { available: false, reason: "unreadable" };
  const generatedAtMs = Date.parse(parsed.data.generated_at);
  if (!Number.isFinite(generatedAtMs)) return { available: false, reason: "unreadable" };
  if (now.getTime() - generatedAtMs > SERVING_STATUS_STALE_MS) return { available: false, reason: "stale" };
  return { available: true, ...parsed.data };
}

async function readJsonBody(request: Request): Promise<unknown> {
  try {
    return await request.json();
  } catch {
    return {};
  }
}

export function registerServingControlRoutes(app: Hono): void {
  app.post("/api/serving/visibility", async (c) => {
    const body = await readJsonBody(c.req.raw);
    const result = recordServingVisibilityAction(config.controlRequestsDir, body);
    return c.json(result.body, result.status);
  });
  app.get("/api/serving/status", (c) => {
    return c.json(readServingStatus(config.controlHostDir));
  });
}
