import type { CaptureEnvelope, CaptureRecord } from "@bench-dashboard/shared";
import type { Hono } from "hono";
import { config } from "../config.ts";
import { getCaptureRecord, listExperimentIds } from "../db/experiments.ts";
import { readServingConfig } from "../serving/llamaConfig.ts";

// `GET /api/export/scoreboard-capture` — raw record pass-through, the FROZEN v1 capture
// contract consumed by `model_scoreboard`'s `local-capture` source adapter. `captureVersion: 1`
// and `stability: "v1"` are both on the wire; this route accepts an optional
// `?id=<experimentId>` filter (repeatable) but otherwise returns every ingested experiment.
// Any future envelope field removal/rename, or a verbatim-record-field change that breaks
// the consumer's zod read, bumps `captureVersion` together with the consumer's literal.
//
// `serving` (WP-3, `plans/served-recipe-p50-prefill-and-decode.md`) is the one additive
// exception: this route reads `config.llamaConfigPath` ONCE per request (via
// `readServingConfig`, the same reader `routes/servingTiming.ts` uses, and the same
// try/catch -> 502-on-read-failure posture) and passes the resulting alias list into every
// `getCaptureRecord` call, so every record in one response resolves against the identical
// config snapshot. A config read failure fails the whole export request (502) rather than
// silently omitting `serving` from some records.
export function registerExportRoutes(app: Hono): void {
  app.get("/api/export/scoreboard-capture", async (c) => {
    try {
      const servingConfig = await readServingConfig(config.llamaConfigPath);
      const requestedIds = c.req.queries("id");
      const ids = requestedIds && requestedIds.length > 0 ? requestedIds : listExperimentIds();
      const records: CaptureRecord[] = [];
      for (const id of ids) {
        const record = getCaptureRecord(id, servingConfig.models);
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
    } catch (error) {
      return c.json({ error: error instanceof Error ? error.message : String(error) }, 502);
    }
  });
}
