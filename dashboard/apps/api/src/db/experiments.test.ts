import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import type { ServingAlias } from "@bench-dashboard/shared";
import { useTemporaryDatabase } from "./testDatabase.ts";

// Module-body call, before any other import reaching `db/connection.ts` (see
// `testDatabase.ts`'s header comment for why this must happen here).
await useTemporaryDatabase("experiments-projections");

const { replaceExperiment, listExperimentSummaries, getExperimentDetail, listServingTimingRows, percentile } =
  await import("./experiments.ts");
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


// WP-1 (`plans/served-recipe-p50-prefill-and-decode.md`): `percentile`'s regression lock
// against the pre-WP-1 `median` (WP-3, `bakeoff-timing-records-and-estimates.md`) -- every
// existing `median` caller (`listLadderRows` above) is `percentile(values, 0.5)` internally
// now, so these hand-computed values ARE what `median` always returned for the same input.
describe("percentile: regression lock vs the old median, plus real p90 math", () => {
  test("n=1: the single value, for p=0.5 (old median's only case for n=1)", () => {
    expect(percentile([42], 0.5)).toBe(42);
  });

  test("n=1: the single value for ANY p, incl. p=0.9 (Decisions locked: n=1 returns the value for every p)", () => {
    expect(percentile([42], 0.9)).toBe(42);
    expect(percentile([42], 0.1)).toBe(42);
  });

  test("n=2: p50 averages the two values, matching old median's even-count branch", () => {
    expect(percentile([10, 20], 0.5)).toBe(15);
  });

  test("n=3: p50 picks the middle value, matching old median's odd-count branch", () => {
    expect(percentile([30, 10, 20], 0.5)).toBe(20);
  });

  test("n=10: p50 averages the two middle values, matching old median's even-count branch", () => {
    expect(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5)).toBe(5.5);
  });

  test("n=3: hand-computed p90 (rank 1.8, interpolated 20/30 at weight 0.8)", () => {
    expect(percentile([10, 20, 30], 0.9)).toBe(28);
  });

  test("empty sample: null, never a guessed value, for any p", () => {
    expect(percentile([], 0.5)).toBeNull();
    expect(percentile([], 0.9)).toBeNull();
  });
});

// WP-1: `listServingTimingRows(servedAliases)`'s aggregate-correctness cases -- rekeyed
// from a raw `sweep_alias` group to the served-recipe resolver's `modelKey`. Raw SQL
// inserts, not `replaceExperiment` -- that helper always writes the `schema_origin` DDL
// default `'v2'` and has no parameter for `sampling_arm`/`sweep_server_flags`/
// `model_host_path` together in one call convenient for these cases -- same discipline
// `ingest/legacyTiming.test.ts` uses for its own verification helpers. Each case below uses
// its OWN model path/served alias (never shared across tests) -- this file's temp DB is
// shared across every test in it, and `listServingTimingRows` scans every `timing_round`
// row in the DB unconditionally before resolving, so a shared path would let an earlier
// test's rounds leak into a later one's arm group.
function insertTimingExperiment(
  id: string,
  opts: {
    modelHostPath?: string | null;
    sweepAlias?: string | null;
    schemaOrigin?: "v2" | "v1-timing";
    samplingArm?: string | null;
    sweepServerFlags?: unknown;
  } = {},
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

/** One `timing_round` metric row. `prefill` lands in `raw_json.server_prompt_eval_tok_s`
 * (the real shape `json_extract` reads) -- `undefined` (never passed) means the round's
 * raw payload simply doesn't carry the key at all, the real "prefill wasn't recorded for
 * this round" case. */
function insertTimingRound(experimentId: string, decode: number | null, prefill?: number): void {
  const raw = prefill === undefined ? {} : { server_prompt_eval_tok_s: prefill };
  db.query(
    `INSERT INTO metrics (experiment_id, metric_type, observed_decode_tok_s, raw_json) VALUES (?, 'timing_round', ?, ?)`,
  ).run(experimentId, decode, JSON.stringify(raw));
}

/** A plain-recipe (no MTP, f16/f16 KV, no offload) served model at `modelPath` -- a record
 * whose `model_host_path` matches AND whose flags reduce to the same all-defaults recipe
 * (no `sweep_server_flags` at all) gets `recipeMatch: true` against it. */
function plainServedAlias(modelKey: string, modelPath: string): ServingAlias {
  return {
    modelKey,
    name: null,
    aliases: [`${modelKey}-alias`],
    ttl: null,
    checkEndpoint: null,
    flags: { contextLength: null, kvCacheTypeK: null, kvCacheTypeV: null, flashAttention: null, gpuLayers: null, allFlags: [] },
    capability: null,
    recipe: { modelPath, draftPath: null, specType: null, moeCpuLayers: null, kvK: "f16", kvV: "f16" },
  };
}

describe("listServingTimingRows: served-recipe-resolved p50/p90 prefill + decode (WP-1)", () => {
  test("production-only: the sole arm present is picked, decode+prefill percentiles computed over its rounds", () => {
    const path = "/models/fixture-org/fixture-timing-prod-only-GGUF/model.gguf";
    const alias = plainServedAlias("fixture-timing-prod-only", path);
    insertTimingExperiment("exp-timing-prod-only", { modelHostPath: path, samplingArm: "production" });
    insertTimingRound("exp-timing-prod-only", 10, 100);
    insertTimingRound("exp-timing-prod-only", 20, 200);
    insertTimingRound("exp-timing-prod-only", 30, 300);

    const rows = listServingTimingRows([alias]);
    const row = rows.find((r) => r.modelKey === "fixture-timing-prod-only");
    expect(row).toMatchObject({
      aliases: ["fixture-timing-prod-only-alias"],
      samplingArm: "production",
      sampleCount: 3,
      p50DecodeTokS: 20,
      p50PrefillTokS: 200,
      mtp: false,
    });
  });

  test("capability-only: the sole arm present is picked when it's capability, not production", () => {
    const path = "/models/fixture-org/fixture-timing-cap-only-GGUF/model.gguf";
    const alias = plainServedAlias("fixture-timing-cap-only", path);
    insertTimingExperiment("exp-timing-cap-only", { modelHostPath: path, samplingArm: "capability" });
    insertTimingRound("exp-timing-cap-only", 40);
    insertTimingRound("exp-timing-cap-only", 60);

    const rows = listServingTimingRows([alias]);
    const row = rows.find((r) => r.modelKey === "fixture-timing-cap-only");
    expect(row).toMatchObject({ samplingArm: "capability", sampleCount: 2, p50DecodeTokS: 50 });
  });

  test("both present: production wins over capability (never pooled)", () => {
    const path = "/models/fixture-org/fixture-timing-both-GGUF/model.gguf";
    const alias = plainServedAlias("fixture-timing-both", path);
    insertTimingExperiment("exp-timing-both-prod", { modelHostPath: path, samplingArm: "production" });
    insertTimingRound("exp-timing-both-prod", 5);
    insertTimingExperiment("exp-timing-both-cap", { modelHostPath: path, samplingArm: "capability" });
    insertTimingRound("exp-timing-both-cap", 999);

    const rows = listServingTimingRows([alias]);
    const row = rows.find((r) => r.modelKey === "fixture-timing-both");
    // Never pooled: the capability arm's 999 never enters this row's decode sample.
    expect(row).toMatchObject({ samplingArm: "production", sampleCount: 1, p50DecodeTokS: 5 });
  });

  test("null-only (unlabelled): picked when it's the only arm present", () => {
    const path = "/models/fixture-org/fixture-timing-null-only-GGUF/model.gguf";
    const alias = plainServedAlias("fixture-timing-null-only", path);
    insertTimingExperiment("exp-timing-null-only", { modelHostPath: path, samplingArm: null });
    insertTimingRound("exp-timing-null-only", 77);

    const rows = listServingTimingRows([alias]);
    const row = rows.find((r) => r.modelKey === "fixture-timing-null-only");
    expect(row).toMatchObject({ samplingArm: null, sampleCount: 1, p50DecodeTokS: 77 });
  });

  test("zero admissible rounds (recipeMatch:false -- off-recipe flags) never produces a row, never a computed zero", () => {
    const path = "/models/fixture-org/fixture-timing-off-recipe-GGUF/model.gguf";
    const alias = plainServedAlias("fixture-timing-off-recipe", path);
    insertTimingExperiment("exp-timing-off-recipe", {
      modelHostPath: path,
      samplingArm: "production",
      sweepServerFlags: { "-ctk": "q4_0", "-ctv": "q4_0" }, // served recipe is f16/f16 -- mismatch
    });
    insertTimingRound("exp-timing-off-recipe", 999);

    const rows = listServingTimingRows([alias]);
    expect(rows.some((r) => r.modelKey === "fixture-timing-off-recipe")).toBe(false);
  });

  test("a served model with no matching rows at all is simply absent (never a guessed empty row)", () => {
    const alias = plainServedAlias("fixture-timing-unused", "/models/fixture-org/fixture-timing-unused-GGUF/model.gguf");
    const rows = listServingTimingRows([alias]);
    expect(rows.some((r) => r.modelKey === "fixture-timing-unused")).toBe(false);
  });

  test("prefill percentiles are null when no round in the winning arm carries a prefill sample", () => {
    const path = "/models/fixture-org/fixture-timing-no-prefill-GGUF/model.gguf";
    const alias = plainServedAlias("fixture-timing-no-prefill", path);
    insertTimingExperiment("exp-timing-no-prefill", { modelHostPath: path, samplingArm: "production" });
    insertTimingRound("exp-timing-no-prefill", 15); // no prefill argument at all

    const rows = listServingTimingRows([alias]);
    const row = rows.find((r) => r.modelKey === "fixture-timing-no-prefill");
    expect(row?.p50PrefillTokS).toBeNull();
    expect(row?.p90PrefillTokS).toBeNull();
    expect(row?.p50DecodeTokS).toBe(15);
  });
});
