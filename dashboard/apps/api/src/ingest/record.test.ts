import { describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
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

// Two-level ingest (WP-A, `plans/bench-dashboard-run-context-surfacing.md` WP-A): a
// first-level dir with `config.json` stays a flat record dir; a first-level dir WITHOUT
// one, whose immediate subdirs have it, is a batch run dir whose subdirs are scanned
// instead (exactly one extra level, mirroring `llama/bakeoff/records/wp5-validation/<cell-id>/` —
// not depended on here, this fixture tree mirrors its shape); a dir with neither stays
// itself (and downstream `readRecordDir` classifies it `invalid`, unchanged).
describe("listRecordDirs: two-level (batch run dir) scan", () => {
  const TWO_LEVEL_DIR = join(import.meta.dir, "../../fixtures/records-two-level");

  test("scans flat-record, batch-run's two cell subdirs, and neither-dir/nested-too-deep as themselves", async () => {
    const dirs = await listRecordDirs(TWO_LEVEL_DIR);
    const relative = dirs.map((d) => d.slice(TWO_LEVEL_DIR.length + 1)).sort();
    expect(relative).toEqual(["batch-run/cell-a", "batch-run/cell-b", "flat-record", "neither-dir", "nested-too-deep"]);
  });

  test("a flat record dir (config.json at first level) is returned as itself, not expanded", async () => {
    const dirs = await listRecordDirs(TWO_LEVEL_DIR);
    expect(dirs).toContain(join(TWO_LEVEL_DIR, "flat-record"));
  });

  test("a batch run dir's cell subdirs are returned individually, never the batch dir itself", async () => {
    const dirs = await listRecordDirs(TWO_LEVEL_DIR);
    expect(dirs).not.toContain(join(TWO_LEVEL_DIR, "batch-run"));
    expect(dirs).toContain(join(TWO_LEVEL_DIR, "batch-run", "cell-a"));
    expect(dirs).toContain(join(TWO_LEVEL_DIR, "batch-run", "cell-b"));
  });

  test("a dir with neither config.json nor a config.json-bearing subdir is returned as itself (invalid downstream)", async () => {
    const dirs = await listRecordDirs(TWO_LEVEL_DIR);
    expect(dirs).toContain(join(TWO_LEVEL_DIR, "neither-dir"));
    const result = await readRecordDir(join(TWO_LEVEL_DIR, "neither-dir"));
    expect(result.kind).toBe("invalid");
  });

  test("nested-deeper-is-ignored: a config.json three levels down is never discovered", async () => {
    const dirs = await listRecordDirs(TWO_LEVEL_DIR);
    // `nested-too-deep/inner/deeper/config.json` exists, but `nested-too-deep`'s own
    // immediate subdir (`inner`) has no config.json directly -- never classified batch.
    expect(dirs).toContain(join(TWO_LEVEL_DIR, "nested-too-deep"));
    expect(dirs).not.toContain(join(TWO_LEVEL_DIR, "nested-too-deep", "inner"));
    expect(dirs).not.toContain(join(TWO_LEVEL_DIR, "nested-too-deep", "inner", "deeper"));
    const result = await readRecordDir(join(TWO_LEVEL_DIR, "nested-too-deep"));
    expect(result.kind).toBe("invalid");
  });
});

// Ingest population (WP-A item 3): sweep_cell.sampling_arm/battery and top-level
// vram_profile normalize onto the record; absent stays null (existing flat fixtures,
// which predate this WP, cover the absent case without modification).
describe("readRecordDir: sampling_arm/battery/vram_profile population", () => {
  const PROJECTIONS_DIR = join(import.meta.dir, "../../fixtures/records-projections");

  test("populates samplingArm/battery/vramProfile when present on the record", async () => {
    const result = await readRecordDir(join(PROJECTIONS_DIR, "fixture-v2-battery"));
    expect(result.kind).toBe("v2");
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.samplingArm).toBe("production");
    expect(result.record.battery).toEqual(["needle", "vram"]);
    expect(result.record.vramProfile).toBe("desktop-shared");
  });

  // WP-1 (`plans/served-recipe-p50-prefill-and-decode.md`): `sweep_cell.model_host_path`
  // promotes onto `NormalizedRecord.modelHostPath` the same way its siblings above do.
  test("promotes modelHostPath when sweep_cell.model_host_path is present", async () => {
    const result = await readRecordDir(join(PROJECTIONS_DIR, "fixture-v2-battery"));
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.modelHostPath).toBe("/models/example-org/Example-Battery-GGUF/Example-Battery-Q4_K_M.gguf");
  });

  test("modelHostPath stays null when sweep_cell carries no model_host_path (fixture-v2-smoke-cell)", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "fixture-v2-smoke-cell"));
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.sweepCellId).not.toBeNull(); // sweep_cell IS present on this fixture
    expect(result.record.modelHostPath).toBeNull();
  });

  test("absent sweep_cell (fixture-v2-full: sweep_cell itself null) stays null throughout", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "fixture-v2-full"));
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.samplingArm).toBeNull();
    expect(result.record.battery).toBeNull();
    expect(result.record.vramProfile).toBeNull();
    expect(result.record.modelHostPath).toBeNull();
  });

  test("present sweep_cell but absent sampling_arm/battery sub-fields (fixture-v2-smoke-cell) stays null", async () => {
    const result = await readRecordDir(join(FIXTURES_DIR, "fixture-v2-smoke-cell"));
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.sweepCellId).not.toBeNull(); // sweep_cell IS present on this fixture
    expect(result.record.samplingArm).toBeNull();
    expect(result.record.battery).toBeNull();
    expect(result.record.vramProfile).toBeNull();
  });
});

// WP-R5 (`plans/repository-tooling-structural-remediation.md`, review F5
// boundary-type-contracts): `config.wilson_summary` is now parsed against the shared
// `WilsonSummarySchema` during v2 ingest -- malformed NON-NULL data makes the whole record
// `invalid` (never a plausible-looking null-filled `summaries` row) while valid explicit
// `null` (the frozen contract's "no replicated correctness cells" case) keeps ingesting
// exactly as before. These build their own throwaway record dirs with `mkdtempSync`
// (the committed `fixtures/records` tree is historical evidence, never edited for a new
// case) rather than reusing the existing fixtures.
describe("readRecordDir: wilson_summary validation (WP-R5)", () => {
  function makeRecordDir(wilsonSummary: unknown): string {
    const dir = mkdtempSync(join(tmpdir(), "bench-dashboard-wilson-"));
    writeFileSync(
      join(dir, "config.json"),
      JSON.stringify({
        schema_version: 2,
        description: "WP-R5 wilson_summary validation fixture",
        timestamp: "2026-08-27T00:00:00+00:00",
        lineage: {},
        sweep_cell: null,
        wilson_summary: wilsonSummary,
      }),
    );
    // Only reached by the valid-null case below -- a malformed non-null summary makes
    // `readRecordDir` return `invalid` before it ever reads these (`record.ts`'s
    // wilson_summary parse runs immediately after the schema_version gate, ahead of the
    // prompts/completions/samples reads), but writing them unconditionally keeps this
    // helper's contract simple and matches what a real v2 record dir always has on disk.
    writeFileSync(join(dir, "prompts.jsonl"), "");
    writeFileSync(join(dir, "completions.jsonl"), "");
    writeFileSync(join(dir, "samples.jsonl"), "");
    return dir;
  }

  test("a malformed wilson_summary (n is a string, not a number) becomes an invalid record", async () => {
    const dir = makeRecordDir({ n: "thirty", k: 0, rate: 0, sided: "one", confidence: 0.95, lower: null, upper: null });
    const result = await readRecordDir(dir);
    expect(result.kind).toBe("invalid");
    if (result.kind !== "invalid") throw new Error("unreachable");
    expect(result.reason).toContain("wilson_summary");
  });

  test("a malformed wilson_summary (missing required field k) becomes an invalid record", async () => {
    const dir = makeRecordDir({ n: 30, rate: 0, sided: "one", confidence: 0.95, lower: null, upper: null });
    const result = await readRecordDir(dir);
    expect(result.kind).toBe("invalid");
  });

  test("an out-of-vocabulary wilson_summary.sided (\"three\") becomes an invalid record", async () => {
    const dir = makeRecordDir({ n: 30, k: 0, rate: 0, sided: "three", confidence: 0.95, lower: null, upper: null });
    const result = await readRecordDir(dir);
    expect(result.kind).toBe("invalid");
    if (result.kind !== "invalid") throw new Error("unreachable");
    expect(result.reason).toContain("wilson_summary");
  });

  test("a valid explicit-null wilson_summary keeps ingesting as v2, unchanged (compatibility)", async () => {
    const dir = makeRecordDir(null);
    const result = await readRecordDir(dir);
    expect(result.kind).toBe("v2");
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.wilsonSummary).toBeNull();
  });

  test("a valid non-null wilson_summary object still ingests as v2 through the new parse path", async () => {
    const valid = { n: 30, k: 3, rate: 0.1, sided: "two" as const, confidence: 0.95, lower: 0.02, upper: 0.27 };
    const dir = makeRecordDir(valid);
    const result = await readRecordDir(dir);
    expect(result.kind).toBe("v2");
    if (result.kind !== "v2") throw new Error("unreachable");
    expect(result.record.wilsonSummary).toEqual(valid);
  });
});
