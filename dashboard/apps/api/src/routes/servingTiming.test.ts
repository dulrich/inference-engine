import { describe, expect, test } from "bun:test";
import { Hono } from "hono";
import { join } from "node:path";
import { useTemporaryDatabase } from "../db/testDatabase.ts";

// `LLAMA_CONFIG_PATH` must be set BEFORE `config.ts` first loads (transitively, via
// `servingTiming.ts`'s own import of it) -- same ordering requirement `app.routes.test.ts`
// documents. This route now reads the served-alias list per request (WP-1, `plans/
// served-recipe-p50-prefill-and-decode.md`) so its own aggregate can resolve rows against
// it; this file never reads the real `llama/config.yaml` (brief hard constraint).
process.env.LLAMA_CONFIG_PATH = join(import.meta.dir, "../../fixtures/llama-config.yaml");

// Own temp DB (`routes-serving-timing` -- distinct from every other suite's label) so this
// route's wiring/shape/empty-DB assertions never entangle with another file's row counts.
// This suite is deliberately thin: the route itself is a one-line wrapper over
// `readServingConfig` + `listServingTimingRows` (`db/experiments.ts`), and the substantive
// aggregate-correctness cases (percentile math, arm preference, the resolver's resolution
// order) live in `db/experiments.test.ts`/`serving/recipe.test.ts` instead -- same division
// of labor `routes/queue.test.ts` documents for `buildQueueListResponse` vs. its route.
// `app.routes.test.ts` separately confirms `registerServingTimingRoutes` is wired into the
// real `app.ts` before the SPA wildcard (audit cycle-1 B2) -- this file exercises the route
// handler directly against a fresh `Hono` app instead, so it never depends on `config.ts`'s
// process-wide singleton for anything beyond `llamaConfigPath` (set above).

await useTemporaryDatabase("routes-serving-timing");

const { registerServingTimingRoutes } = await import("./servingTiming.ts");
const { db } = await import("../db/connection.ts");

function testApp(): Hono {
  const app = new Hono();
  registerServingTimingRoutes(app);
  return app;
}

// The fixture config's `fixture-chat-q4` model: `-m /models/example-org/fixture-chat-gguf/
// fixture-chat-Q4_K_M.gguf -ctk q4_0 -ctv q4_0`, no MTP, no `-ncmoe` -- a record whose own
// flags reduce to the identical tuple gets `recipeMatch: true` against it.
const FIXTURE_CHAT_MODEL_PATH = "/models/example-org/fixture-chat-gguf/fixture-chat-Q4_K_M.gguf";

function insertExperiment(
  id: string,
  opts: { modelHostPath?: string | null; sweepAlias?: string | null; schemaOrigin?: "v2" | "v1-timing"; samplingArm?: string | null; sweepServerFlags?: unknown } = {},
): void {
  db.query(
    `
      INSERT INTO experiments (
        id, schema_version, sweep_alias, sampling_arm, sweep_server_flags, model_host_path,
        raw_config_json, raw_prompts_json, raw_completions_json, ingested_at, schema_origin
      ) VALUES (?, 2, ?, ?, ?, ?, '{}', '[]', '[]', ?, ?)
    `,
  ).run(
    id,
    opts.sweepAlias ?? null,
    opts.samplingArm ?? null,
    opts.sweepServerFlags === undefined ? null : JSON.stringify(opts.sweepServerFlags),
    opts.modelHostPath ?? null,
    new Date().toISOString(),
    opts.schemaOrigin ?? "v2",
  );
}

function insertTimingMetric(experimentId: string, observedDecodeTokS: number | null, prefill?: number): void {
  const raw = prefill === undefined ? {} : { server_prompt_eval_tok_s: prefill };
  db.query(
    `INSERT INTO metrics (experiment_id, metric_type, observed_decode_tok_s, raw_json) VALUES (?, 'timing_round', ?, ?)`,
  ).run(experimentId, observedDecodeTokS, JSON.stringify(raw));
}

interface TimingRowBody {
  modelKey: string;
  aliases: string[];
  p50PrefillTokS: number | null;
  p90PrefillTokS: number | null;
  p50DecodeTokS: number | null;
  p90DecodeTokS: number | null;
  samplingArm: string | null;
  sampleCount: number;
  mtp: boolean;
}

describe("GET /api/serving/timing", () => {
  test("is registered and returns [] on an empty DB", async () => {
    const response = await testApp().request("/api/serving/timing");
    expect(response.status).toBe(200);
    const body = (await response.json()) as TimingRowBody[];
    expect(body).toEqual([]);
  });

  test("shape: one row per served model, keyed by modelKey, with p50/p90 prefill+decode and provenance", async () => {
    insertExperiment("exp-timing-shape", { modelHostPath: FIXTURE_CHAT_MODEL_PATH, samplingArm: "production", sweepServerFlags: { "-ctk": "q4_0", "-ctv": "q4_0" } });
    insertTimingMetric("exp-timing-shape", 40, 400);
    insertTimingMetric("exp-timing-shape", 50, 500);

    const response = await testApp().request("/api/serving/timing");
    expect(response.status).toBe(200);
    const body = (await response.json()) as TimingRowBody[];
    const row = body.find((r) => r.modelKey === "fixture-chat-q4");
    expect(row).toBeDefined();
    expect(row?.aliases).toEqual(["fixture-chat"]);
    expect(row?.p50DecodeTokS).toBe(45);
    expect(row?.p50PrefillTokS).toBe(450);
    expect(row?.samplingArm).toBe("production");
    expect(row?.sampleCount).toBe(2);
    expect(row?.mtp).toBe(false);
  });

  test("a row whose measured recipe doesn't match the served recipe never contributes -- nothing to guess", async () => {
    insertExperiment("exp-timing-off-recipe", { modelHostPath: FIXTURE_CHAT_MODEL_PATH, samplingArm: "production", sweepServerFlags: {} });
    insertTimingMetric("exp-timing-off-recipe", 999);

    const response = await testApp().request("/api/serving/timing");
    const body = (await response.json()) as TimingRowBody[];
    expect(body.some((r) => r.sampleCount > 0 && r.p50DecodeTokS === 999)).toBe(false);
  });

  test("an experiment with no model_host_path and a v2 schema_origin never contributes -- nothing to join on", async () => {
    insertExperiment("exp-timing-no-path", { modelHostPath: null, sweepAlias: "fixture-scratch-alias", schemaOrigin: "v2" });
    insertTimingMetric("exp-timing-no-path", 99);

    const response = await testApp().request("/api/serving/timing");
    const body = (await response.json()) as TimingRowBody[];
    expect(body.some((r) => r.p50DecodeTokS === 99)).toBe(false);
  });
});
