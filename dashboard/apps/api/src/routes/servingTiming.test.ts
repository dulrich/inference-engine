import { describe, expect, test } from "bun:test";
import { Hono } from "hono";
import { useTemporaryDatabase } from "../db/testDatabase.ts";

// Own temp DB (`routes-serving-timing` -- distinct from every other suite's label) so this
// route's wiring/shape/empty-DB assertions never entangle with another file's row counts.
// This suite is deliberately thin: the route itself is a one-line wrapper over
// `listServingTimingRows` (`db/experiments.ts`), and the substantive aggregate-correctness
// cases (median math, the v1-timing `-full`-suffix mapping, the v2/legacy merge case) live
// in `db/experiments.test.ts` instead -- same division of labor `routes/queue.test.ts`
// documents for `buildQueueListResponse` vs. its route. `app.routes.test.ts` separately
// confirms `registerServingTimingRoutes` is wired into the real `app.ts` before the SPA
// wildcard (audit cycle-1 B2) -- this file exercises the route handler directly against a
// fresh `Hono` app instead, so it never depends on `config.ts`'s process-wide singleton.

await useTemporaryDatabase("routes-serving-timing");

const { registerServingTimingRoutes } = await import("./servingTiming.ts");
const { db } = await import("../db/connection.ts");

function testApp(): Hono {
  const app = new Hono();
  registerServingTimingRoutes(app);
  return app;
}

function insertExperiment(id: string, sweepAlias: string | null, schemaOrigin: "v2" | "v1-timing"): void {
  db.query(
    `
      INSERT INTO experiments (id, schema_version, sweep_alias, raw_config_json, raw_prompts_json, raw_completions_json, ingested_at, schema_origin)
      VALUES (?, 2, ?, '{}', '[]', '[]', ?, ?)
    `,
  ).run(id, sweepAlias, new Date().toISOString(), schemaOrigin);
}

function insertTimingMetric(experimentId: string, observedDecodeTokS: number | null): void {
  db.query(
    `INSERT INTO metrics (experiment_id, metric_type, observed_decode_tok_s, raw_json) VALUES (?, 'timing_round', ?, '{}')`,
  ).run(experimentId, observedDecodeTokS);
}

type TimingRowBody = { alias: string; p50DecodeTokS: number | null; sampleCount: number };

describe("GET /api/serving/timing", () => {
  test("is registered and returns [] on an empty DB", async () => {
    const response = await testApp().request("/api/serving/timing");
    expect(response.status).toBe(200);
    const body = (await response.json()) as TimingRowBody[];
    expect(body).toEqual([]);
  });

  test("shape: one row per alias with numeric p50DecodeTokS and sampleCount", async () => {
    insertExperiment("exp-timing-shape", "fixture-serving-timing-alias", "v2");
    insertTimingMetric("exp-timing-shape", 40);
    insertTimingMetric("exp-timing-shape", 50);

    const response = await testApp().request("/api/serving/timing");
    expect(response.status).toBe(200);
    const body = (await response.json()) as TimingRowBody[];
    const row = body.find((r) => r.alias === "fixture-serving-timing-alias");
    expect(row).toBeDefined();
    expect(row?.p50DecodeTokS).toBe(45);
    expect(row?.sampleCount).toBe(2);
  });

  test("an experiment with no sweep_alias never contributes a row -- nothing to join on", async () => {
    insertExperiment("exp-timing-no-alias", null, "v2");
    insertTimingMetric("exp-timing-no-alias", 99);

    const response = await testApp().request("/api/serving/timing");
    const body = (await response.json()) as TimingRowBody[];
    expect(body.some((r) => r.sampleCount === 1 && r.p50DecodeTokS === 99)).toBe(false);
  });

  test("a timing_round row with a null observed_decode_tok_s is never counted as a sample", async () => {
    insertExperiment("exp-timing-null-sample", "fixture-serving-timing-null-alias", "v2");
    insertTimingMetric("exp-timing-null-sample", null);

    const response = await testApp().request("/api/serving/timing");
    const body = (await response.json()) as TimingRowBody[];
    expect(body.some((r) => r.alias === "fixture-serving-timing-null-alias")).toBe(false);
  });
});
