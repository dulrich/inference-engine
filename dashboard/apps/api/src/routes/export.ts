import type { CaptureEnvelope, CaptureRecord } from "@bench-dashboard/shared";
import type { Hono } from "hono";
import { getCaptureRecord, listExperimentIds } from "../db/experiments.ts";

// `GET /api/export/scoreboard-capture` — raw record pass-through for the deferred
// `model_scoreboard` connector successor plan (Decisions locked). Explicitly EXPERIMENTAL,
// not a stable public interface (cycle-1 material finding M4): the envelope's own
// `stability` field says so on the wire, and this route accepts an optional
// `?id=<experimentId>` filter (repeatable) but otherwise returns every ingested experiment
// — the successor plan is what freezes a versioned contract here, not this WP.
export function registerExportRoutes(app: Hono): void {
  app.get("/api/export/scoreboard-capture", (c) => {
    const requestedIds = c.req.queries("id");
    const ids = requestedIds && requestedIds.length > 0 ? requestedIds : listExperimentIds();
    const records: CaptureRecord[] = [];
    for (const id of ids) {
      const record = getCaptureRecord(id);
      if (record) records.push(record);
    }
    const envelope: CaptureEnvelope = {
      stability: "experimental",
      generatedAt: new Date().toISOString(),
      recordCount: records.length,
      records,
    };
    return c.json(envelope);
  });
}
