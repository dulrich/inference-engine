import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { useTemporaryDatabase } from "./testDatabase.ts";

// Module-body call, before any other import reaching `db/connection.ts` (see
// `testDatabase.ts`'s header comment for why this must happen here).
await useTemporaryDatabase("experiments-projections");

const { replaceExperiment, listExperimentSummaries, getExperimentDetail, listServingTimingRows } = await import(
  "./experiments.ts"
);
const { readRecordDir } = await import("../ingest/record.ts");
const { db } = await import("./connection.ts");

const RECORDS_DIR = join(import.meta.dir, "../../fixtures/records");
const PROJECTIONS_DIR = join(import.meta.dir, "../../fixtures/records-projections");

// Projection round-trips (WP-A item 4, cycle-2 audit M1 -- "adding a DB column alone
// exposes nothing", so every promoted column is exercised through BOTH the summary list
// AND the detail read here) incl. null cases (a record predating this WP).
describe("experiment projections: samplingArm/battery/vramProfile/responseCaps", () => {
  test("round-trips a full record's sampling_arm/battery/vram_profile through summary and detail", async () => {
    const result = await readRecordDir(join(PROJECTIONS_DIR, "fixture-v2-battery"));
    if (result.kind !== "v2") throw new Error("unreachable");
    replaceExperiment(result.record);

    const summary = listExperimentSummaries().find((e) => e.id === "fixture-v2-battery");
    expect(summary).toBeDefined();
    expect(summary?.samplingArm).toBe("production");
    expect(summary?.battery).toEqual(["needle", "vram"]);
    expect(summary?.vramProfile).toBe("desktop-shared");

    const detail = getExperimentDetail("fixture-v2-battery");
    expect(detail).toBeDefined();
    expect(detail?.samplingArm).toBe("production");
    expect(detail?.battery).toEqual(["needle", "vram"]);
    expect(detail?.vramProfile).toBe("desktop-shared");
  });

  test("responseCaps is parsed from raw_config_json.response_caps on detail only", async () => {
    const detail = getExperimentDetail("fixture-v2-battery");
    expect(detail?.responseCaps).toEqual({ needle: 4096, default: 2048 });
    // Not part of the summary shape at all (detail-only, per the brief's pin).
    const summary = listExperimentSummaries().find((e) => e.id === "fixture-v2-battery");
    expect(summary).not.toHaveProperty("responseCaps");
  });

  test("a record predating WP-A (no sampling_arm/battery/vram_profile/response_caps) projects all four as null", async () => {
    const result = await readRecordDir(join(RECORDS_DIR, "fixture-v2-full"));
    if (result.kind !== "v2") throw new Error("unreachable");
    replaceExperiment(result.record);

    const summary = listExperimentSummaries().find((e) => e.id === "fixture-v2-full");
    expect(summary?.samplingArm).toBeNull();
    expect(summary?.battery).toBeNull();
    expect(summary?.vramProfile).toBeNull();

    const detail = getExperimentDetail("fixture-v2-full");
    expect(detail?.samplingArm).toBeNull();
    expect(detail?.battery).toBeNull();
    expect(detail?.vramProfile).toBeNull();
    expect(detail?.responseCaps).toBeNull();
  });

  test("re-ingesting the same record (delete-then-insert) keeps the projections identical", async () => {
    const result = await readRecordDir(join(PROJECTIONS_DIR, "fixture-v2-battery"));
    if (result.kind !== "v2") throw new Error("unreachable");
    replaceExperiment(result.record);
    const detail = getExperimentDetail("fixture-v2-battery");
    expect(detail?.samplingArm).toBe("production");
    expect(detail?.battery).toEqual(["needle", "vram"]);
    expect(detail?.vramProfile).toBe("desktop-shared");
    expect(detail?.responseCaps).toEqual({ needle: 4096, default: 2048 });
  });
});

// WP-3 (`plans/bakeoff-timing-records-and-estimates.md`): `listServingTimingRows`'s
// aggregate-correctness cases. Raw SQL inserts, not `replaceExperiment` -- that helper
// always writes the `schema_origin` DDL default `'v2'` and has no parameter for
// provenance (the column's sole writer is `ingest/run.ts`, out of this WP's file scope) --
// same discipline `ingest/legacyTiming.test.ts` uses for its own verification helpers.
function insertServingTimingExperiment(id: string, sweepAlias: string | null, schemaOrigin: "v2" | "v1-timing"): void {
  db.query(
    `
      INSERT INTO experiments (id, schema_version, sweep_alias, raw_config_json, raw_prompts_json, raw_completions_json, ingested_at, schema_origin)
      VALUES (?, 2, ?, '{}', '[]', '[]', ?, ?)
    `,
  ).run(id, sweepAlias, new Date().toISOString(), schemaOrigin);
}
function insertServingTimingMetric(experimentId: string, observedDecodeTokS: number): void {
  db.query(
    `INSERT INTO metrics (experiment_id, metric_type, observed_decode_tok_s, raw_json) VALUES (?, 'timing_round', ?, '{}')`,
  ).run(experimentId, observedDecodeTokS);
}

describe("listServingTimingRows: p50 + sample count aggregate (WP-3)", () => {
  test("median: odd sample count picks the middle value, even count averages the two middles", () => {
    insertServingTimingExperiment("exp-timing-odd", "fixture-servingtiming-odd-alias", "v2");
    insertServingTimingMetric("exp-timing-odd", 10);
    insertServingTimingMetric("exp-timing-odd", 30);
    insertServingTimingMetric("exp-timing-odd", 20);

    insertServingTimingExperiment("exp-timing-even", "fixture-servingtiming-even-alias", "v2");
    insertServingTimingMetric("exp-timing-even", 10);
    insertServingTimingMetric("exp-timing-even", 20);
    insertServingTimingMetric("exp-timing-even", 30);
    insertServingTimingMetric("exp-timing-even", 40);

    const rows = listServingTimingRows();
    expect(rows.find((r) => r.alias === "fixture-servingtiming-odd-alias")).toMatchObject({
      p50DecodeTokS: 20,
      sampleCount: 3,
    });
    expect(rows.find((r) => r.alias === "fixture-servingtiming-even-alias")).toMatchObject({
      p50DecodeTokS: 25,
      sampleCount: 4,
    });
  });

  test("legacy-suffix mapping: a v1-timing alias has its trailing -full suffix stripped", () => {
    insertServingTimingExperiment("exp-timing-legacy-full", "fixture-servingtiming-legacy-full", "v1-timing");
    insertServingTimingMetric("exp-timing-legacy-full", 42);

    const rows = listServingTimingRows();
    expect(rows.some((r) => r.alias === "fixture-servingtiming-legacy-full")).toBe(false);
    expect(rows.find((r) => r.alias === "fixture-servingtiming-legacy")).toMatchObject({
      p50DecodeTokS: 42,
      sampleCount: 1,
    });
  });

  test("the -full suffix is never stripped from a v2 alias", () => {
    insertServingTimingExperiment("exp-timing-v2-full-literal", "fixture-servingtiming-v2-literal-full", "v2");
    insertServingTimingMetric("exp-timing-v2-full-literal", 55);

    const rows = listServingTimingRows();
    expect(rows.find((r) => r.alias === "fixture-servingtiming-v2-literal-full")).toMatchObject({
      p50DecodeTokS: 55,
      sampleCount: 1,
    });
    expect(rows.some((r) => r.alias === "fixture-servingtiming-v2-literal")).toBe(false);
  });

  test("merge case: a stripped legacy alias colliding with a v2 alias merges both sample sets", () => {
    insertServingTimingExperiment("exp-timing-merge-legacy-full", "fixture-servingtiming-merge-full", "v1-timing");
    insertServingTimingMetric("exp-timing-merge-legacy-full", 10);
    insertServingTimingExperiment("exp-timing-merge-v2", "fixture-servingtiming-merge", "v2");
    insertServingTimingMetric("exp-timing-merge-v2", 20);

    const rows = listServingTimingRows();
    expect(rows.find((r) => r.alias === "fixture-servingtiming-merge")).toMatchObject({
      p50DecodeTokS: 15,
      sampleCount: 2,
    });
  });

  test("an alias with zero timing_round samples never appears (null-honest: absence, not a guessed zero)", () => {
    insertServingTimingExperiment("exp-timing-no-samples", "fixture-servingtiming-no-samples-alias", "v2");

    const rows = listServingTimingRows();
    expect(rows.some((r) => r.alias === "fixture-servingtiming-no-samples-alias")).toBe(false);
  });

  test("an experiment with no sweep_alias is excluded -- nothing to join on", () => {
    insertServingTimingExperiment("exp-timing-null-alias", null, "v2");
    insertServingTimingMetric("exp-timing-null-alias", 99);

    const rows = listServingTimingRows();
    expect(rows.some((r) => r.sampleCount === 1 && r.p50DecodeTokS === 99)).toBe(false);
  });
});
