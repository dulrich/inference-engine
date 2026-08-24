import { linkSync, unlinkSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import type { ControlAcceptedResponse, ControlAction } from "@bench-dashboard/shared";
import { ControlCancelRequestSchema, ControlStartRequestSchema } from "@bench-dashboard/shared";
import type { Hono } from "hono";
import { config } from "../config.ts";

// `POST /api/control/{arm,disarm,start,stop,cancel}` (WP-4,
// `plans/bakeoff-queue-control-plane.md`) — each success writes ONE `control-request-v1`
// file into the rw drop-box (`config.controlRequestsDir`) via collision-safe exclusive
// creation (cycle-1 M3: write-temp + `fs.linkSync` + unlink, never a rename over an
// existing path) and returns `{requestId, accepted: true, message?}`. `accepted` means
// *recorded*, never *executed* — the host dispatcher (WP-3, not yet built) is the sole
// authority; the UI learns real outcomes by polling `GET /api/queue`.
//
// Route logic is plain exported functions taking `requestsDir` as an argument (the
// `buildQueueListResponse(queuesDir)` seam pattern) — `config` is a singleton computed
// once at module load and shared across the whole `bun test` process, so tests exercise
// these functions directly against a temp dir rather than fighting that.

const MAX_ID_ATTEMPTS = 5;

export interface ControlRequestFileV1 {
  schema: "control-request-v1";
  request_id: string;
  action: ControlAction;
  queue_stem?: string;
  minutes?: number;
  target_request_id?: string;
  requested_at: string;
}

/** `req-<UTCcompact yyyymmddTHHMMSSZ>-<16 hex from randomUUID>` (plan "Filesystem
 * contract", cycle-1 M3 pin). Exported so tests can exercise the id-collision retry path
 * deterministically (an `idFactory` override on `writeControlRequestExclusive` below),
 * since a real collision under this grammar is not something a test can provoke by luck. */
export function generateRequestId(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const compact =
    `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}T` +
    `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}Z`;
  const hex16 = randomUUID().replace(/-/g, "").slice(0, 16);
  return `req-${compact}-${hex16}`;
}

function isEexist(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && (error as { code?: unknown }).code === "EEXIST";
}

/** Collision-safe exclusive creation (cycle-1 M3): write a temp file in `requestsDir`,
 * then `fs.linkSync(tmp, dst)` — throws `EEXIST` on an id collision, in which case a
 * fresh id is generated and creation is retried (bounded, `MAX_ID_ATTEMPTS`) — then
 * unlink the temp file. Never a destructive rename over an existing request. `idFactory`
 * defaults to `generateRequestId`; overridable so tests can force+observe a collision. */
export function writeControlRequestExclusive(
  requestsDir: string,
  buildBody: (requestId: string, requestedAt: string) => ControlRequestFileV1,
  idFactory: () => string = generateRequestId,
): string {
  for (let attempt = 0; attempt < MAX_ID_ATTEMPTS; attempt++) {
    const requestId = idFactory();
    const requestedAt = new Date().toISOString();
    const body = buildBody(requestId, requestedAt);
    const dst = join(requestsDir, `${requestId}.json`);
    const tmp = join(requestsDir, `.${requestId}.${randomUUID()}.tmp`);
    writeFileSync(tmp, JSON.stringify(body));
    try {
      linkSync(tmp, dst);
    } catch (error) {
      unlinkSync(tmp);
      if (isEexist(error)) continue; // id collision — regenerate and retry
      throw error;
    }
    unlinkSync(tmp);
    return requestId;
  }
  throw new Error(`control request id collided ${MAX_ID_ATTEMPTS} times in a row`);
}

type ControlActionStatus = 200 | 400 | 503;

export interface ControlActionResult {
  status: ControlActionStatus;
  body: ControlAcceptedResponse | { error: string };
}

function unconfiguredResult(): ControlActionResult {
  return {
    status: 503,
    body: { error: "control plane not configured: CONTROL_REQUESTS_DIR is unset" },
  };
}

/** `arm` / `disarm` / `stop` — no request body. `stop`'s `message` carries the
 * delivered-vs-completed caveat (plan wrapper step 7): the marker reaches the live run,
 * but its effect is pause-after-current-cell, or a no-op if the run completes first. */
export function recordSimpleAction(requestsDir: string | undefined, action: "arm" | "disarm" | "stop"): ControlActionResult {
  if (!requestsDir) return unconfiguredResult();
  const requestId = writeControlRequestExclusive(requestsDir, (id, requestedAt) => ({
    schema: "control-request-v1",
    request_id: id,
    action,
    requested_at: requestedAt,
  }));
  if (action === "stop") {
    return {
      status: 200,
      body: {
        requestId,
        accepted: true,
        message:
          "Stop delivered to the live run; effect is pause-after-current-cell, or a no-op if the run completes first.",
      },
    };
  }
  return { status: 200, body: { requestId, accepted: true } };
}

/** `start` — `{queueStem, minutes}`. API pre-validation is UX only (stem regex + minutes
 * [15, 480]); the host dispatcher re-validates fail-closed and is authoritative. */
export function recordStartAction(requestsDir: string | undefined, body: unknown): ControlActionResult {
  if (!requestsDir) return unconfiguredResult();
  const parsed = ControlStartRequestSchema.safeParse(body);
  if (!parsed.success) {
    return { status: 400, body: { error: parsed.error.issues.map((issue) => issue.message).join("; ") } };
  }
  const { queueStem, minutes } = parsed.data;
  const requestId = writeControlRequestExclusive(requestsDir, (id, requestedAt) => ({
    schema: "control-request-v1",
    request_id: id,
    action: "start",
    queue_stem: queueStem,
    minutes,
    requested_at: requestedAt,
  }));
  return { status: 200, body: { requestId, accepted: true } };
}

/** `cancel` — `{targetRequestId}`. Response `message` states the effectiveness window:
 * only before the dispatcher claims the target request (plan cycle-1 B2 lifecycle). */
export function recordCancelAction(requestsDir: string | undefined, body: unknown): ControlActionResult {
  if (!requestsDir) return unconfiguredResult();
  const parsed = ControlCancelRequestSchema.safeParse(body);
  if (!parsed.success) {
    return { status: 400, body: { error: parsed.error.issues.map((issue) => issue.message).join("; ") } };
  }
  const requestId = writeControlRequestExclusive(requestsDir, (id, requestedAt) => ({
    schema: "control-request-v1",
    request_id: id,
    action: "cancel",
    target_request_id: parsed.data.targetRequestId,
    requested_at: requestedAt,
  }));
  return {
    status: 200,
    body: { requestId, accepted: true, message: "Cancellation is effective only before dispatch claim." },
  };
}

async function readJsonBody(request: Request): Promise<unknown> {
  try {
    return await request.json();
  } catch {
    return {};
  }
}

export function registerControlRoutes(app: Hono): void {
  app.post("/api/control/arm", (c) => {
    const result = recordSimpleAction(config.controlRequestsDir, "arm");
    return c.json(result.body, result.status);
  });
  app.post("/api/control/disarm", (c) => {
    const result = recordSimpleAction(config.controlRequestsDir, "disarm");
    return c.json(result.body, result.status);
  });
  app.post("/api/control/stop", (c) => {
    const result = recordSimpleAction(config.controlRequestsDir, "stop");
    return c.json(result.body, result.status);
  });
  app.post("/api/control/start", async (c) => {
    const body = await readJsonBody(c.req.raw);
    const result = recordStartAction(config.controlRequestsDir, body);
    return c.json(result.body, result.status);
  });
  app.post("/api/control/cancel", async (c) => {
    const body = await readJsonBody(c.req.raw);
    const result = recordCancelAction(config.controlRequestsDir, body);
    return c.json(result.body, result.status);
  });
}
