import type { CaptureEnvelope, CaptureRecord } from "@bench-dashboard/shared";
import type { Hono } from "hono";
import { getCaptureRecord, listExperimentIds } from "../db/experiments.ts";

// `GET /api/export/scoreboard-capture` — raw record pass-through, the FROZEN v1 capture
// contract consumed by `model_scoreboard`'s `local-capture` source adapter. `captureVersion: 1`
// and `stability: "v1"` are both on the wire; this route accepts an optional
// `?id=<experimentId>` filter (repeatable) but otherwise returns every ingested experiment.
// Any future envelope field removal/rename, or a record-shape change that breaks the
// consumer's zod read, bumps `captureVersion` together with the consumer's literal.
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
      captureVersion: 1,
      stability: "v1",
      generatedAt: new Date().toISOString(),
      recordCount: records.length,
      records,
    };
    return c.json(envelope);
  });
}
