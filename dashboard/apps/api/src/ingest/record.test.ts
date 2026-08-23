import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { listRecordDirs, readRecordDir } from "./record.ts";

const FIXTURES_DIR = join(import.meta.dir, "../../fixtures/records");

describe("readRecordDir", () => {
  test("classifies a v1 record (no schema_version key) as v1, not invalid", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "fixture-v1-legacy"));
    expect(result.kind).toBe("v1");
  });

  test("classifies and normalizes the smoke-cell v2 record", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "fixture-v2-smoke-cell"));
    expect(result.kind).toBe("v2");
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.id).toBe("fixture-v2-smoke-cell");
    expect(result.record.schemaVersion).toBe(2);
    expect(result.record.hfRepo).toBe("example-org/Example-Tiny-GGUF");
    // The six lineage keys are always present (a value may be null, the key never absent).
    expect(result.record).toHaveProperty("hfRevision");
    expect(result.record).toHaveProperty("sha256");
    // `sweep_cell` present (non-sweep-driven-CLI record's `null` case is covered below).
    expect(result.record.sweepCellId).toBe("fixture-cell-f16-c8192-ctkf16-ctvf16-faon");
    expect(result.record.sweepAlias).toBe("fixture-alias-f16-c8192");
    // Explicit-null `wilson_summary` (deferred audit pin) stays null, never omitted/defaulted.
    expect(result.record.wilsonSummary).toBeNull();
    // Three `sampling_echo_check` samples -> three trials, zero metrics (no timing_round,
    // and the vram snapshot IS present so 3 vram metric rows are expected).
    expect(result.record.trials).toHaveLength(3);
    expect(result.record.trials.every((t) => t.type === "sampling_echo_check")).toBe(true);
    expect(result.record.metrics).toHaveLength(3);
    expect(result.record.metrics.every((m) => m.metricType.startsWith("vram_"))).toBe(true);
  });

  test("normalizes sweep_cell: null (non-sweep full CLI record shape)", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "fixture-v2-full"));
    expect(result.kind).toBe("v2");
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.sweepCellId).toBeNull();
    expect(result.record.sweepAlias).toBeNull();
  });

  test("populates a non-null wilson_summary object verbatim", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "fixture-v2-full"));
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.wilsonSummary).toEqual({
      n: 30,
      k: 0,
      rate: 0,
      sided: "one",
      confidence: 0.95,
      lower: null,
      upper: 0.08266815016452403,
    });
  });

  test("stores a v2 sample type it has never seen before, rather than dropping or throwing", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "fixture-v2-full"));
    if (result.kind !== "v2") throw new Error("unreachable");
    const unknown = result.record.trials.find((t) => t.type === "future_probe_type");
    expect(unknown).toBeDefined();
    expect((unknown?.raw as { some_future_field: number }).some_future_field).toBe(42);
  });

  test("routes timing_round samples to metrics, not trials", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "fixture-v2-full"));
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.trials.some((t) => t.type === "timing_round")).toBe(false);
    expect(result.record.metrics.filter((m) => m.metricType === "timing_round")).toHaveLength(2);
  });

  test("routes longcontext_probe samples to trials with task/position/classifier fields intact", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "fixture-v2-full"));
    if (result.kind !== "v2") throw new Error("unreachable");
    const longcontext = result.record.trials.filter((t) => t.type === "longcontext_probe");
    expect(longcontext).toHaveLength(8);
    expect(longcontext.every((t) => t.task === "single_needle")).toBe(true);
    expect(new Set(longcontext.map((t) => t.positionFraction))).toEqual(new Set([0.1, 0.45]));
  });

  test("an unreadable directory (missing config.json) classifies invalid, not v1 or a throw", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "does-not-exist"));
    expect(result.kind).toBe("invalid");
  });
});

describe("listRecordDirs", () => {
  test("lists exactly the three fixture experiment directories", async () => {
    const dirs = await listRecordDirs(FIXTURES_DIR);
    const names = dirs.map((d) => d.split("/").pop());
    expect(names.sort()).toEqual(["fixture-v1-legacy", "fixture-v2-full", "fixture-v2-smoke-cell"]);
  });
});
