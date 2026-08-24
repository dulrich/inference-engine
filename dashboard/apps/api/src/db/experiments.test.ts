import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { useTemporaryDatabase } from "./testDatabase.ts";

// Module-body call, before any other import reaching `db/connection.ts` (see
// `testDatabase.ts`'s header comment for why this must happen here).
await useTemporaryDatabase("experiments-projections");

const { replaceExperiment, listExperimentSummaries, getExperimentDetail } = await import("./experiments.ts");
const { readRecordDir } = await import("../ingest/record.ts");

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
