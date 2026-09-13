import type { Hono } from "hono";
import { config } from "../config.ts";
import { buildRouterSidecarResponse } from "../router/sidecarFeed.ts";
import { buildRouterSlotsResponse } from "../router/slotsFeed.ts";
import { buildRouterTelemetryResponse } from "../router/telemetryFeed.ts";

// The three read-only router-status routes (`plans/router-observability-and-tool-search-
// conformance.md` WP-3). Registration follows the established pattern
// (`apps/api/src/routes/<name>.ts` exporting `register<Name>Routes(app)`, wired in
// `app.ts`); the config convention is `controlRequestsDir`'s — absence of the env var is
// the feature-off signal, so every route answers 200 with a typed
// `{available: false, reason}` payload and NEVER a 500.
//
// All three read files. None of them makes an outbound HTTP call: the slot data arrives
// via the host unit `dashboard/scripts/router-slots-poller.mjs`, which writes a jsonl this
// container mounts read-only. `serving/llamaConfig.ts:18-33`'s locked decision is intact.

/** `?staleMs=` bounds. Below the sink's 1000 ms progress cadence nothing could ever be
 * fresh; above ten minutes the staleness verdict stops meaning anything. */
export const STALE_MS_MIN = 1000;
export const STALE_MS_MAX = 600_000;

/** An out-of-range, non-numeric or absent `?staleMs=` is IGNORED (each feed falls back to
 * its own default), never a 400 — a status page must not be able to error itself out with
 * a bad query string. */
export function parseStaleMsQuery(raw: string | undefined): number | undefined {
  if (raw === undefined) return undefined;
  const value = Number(raw);
  if (!Number.isFinite(value)) return undefined;
  if (value < STALE_MS_MIN || value > STALE_MS_MAX) return undefined;
  return value;
}

export function registerRouterStatusRoutes(app: Hono): void {
  app.get("/api/router/telemetry", async (c) => {
    const staleMs = parseStaleMsQuery(c.req.query("staleMs"));
    return c.json(await buildRouterTelemetryResponse(config.routerTelemetryDir, { staleMs }));
  });
  // The slots jsonl is written BESIDE the router sink, so it reads the same directory.
  app.get("/api/router/slots", async (c) => {
    const staleMs = parseStaleMsQuery(c.req.query("staleMs"));
    return c.json(await buildRouterSlotsResponse(config.routerTelemetryDir, { staleMs }));
  });
  app.get("/api/router/sidecar", async (c) => {
    return c.json(await buildRouterSidecarResponse(config.codexSidecarLogDir));
  });
}
