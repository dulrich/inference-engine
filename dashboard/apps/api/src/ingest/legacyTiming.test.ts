import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { useTemporaryDatabase } from "../db/testDatabase.ts";

// WP-4 (`plans/bakeoff-timing-records-and-estimates.md`): the allowlisted pre-v2
// timing-only ingest side door. Own temp DB + own fixtures dir (`records-legacy-timing`,
// distinct from the shared `fixtures/records` tree `run.test.ts`/`smoke.ts` depend on) so
// this suite's scan counts never entangle with theirs.

await useTemporaryDatabase("ingest-legacy-timing");

const { db } = await import("../db/connection.ts");
const { listExperimentSummaries, listMetrics, listTrials } = await import("../db/experiments.ts");
const { runIngest } = await import("./run.ts");
const { readLegacyTimingRecord } = await import("./record.ts");

const FIXTURES_DIR = join(import.meta.dir, "../../fixtures/records-legacy-timing");
const LEGACY_FIXTURES_DIR = join(import.meta.dir, "../../fixtures/records");

/** `schema_origin` is not surfaced through `ExperimentSummary`/`ExperimentRow` (that
 * projection belongs to `db/experiments.ts`, WP-3's file) -- read the raw column directly,
 * same discipline `listLadderRows` already uses for `sweep_alias`. */
function schemaOrigin(id: string): string | null {
  const row = db.query(`SELECT schema_origin FROM experiments WHERE id = ?`).get(id) as { schema_origin: string } | null;
  return row?.schema_origin ?? null;
}
function sweepAlias(id: string): string | null {
  const row = db.query(`SELECT sweep_alias FROM experiments WHERE id = ?`).get(id) as { sweep_alias: string | null } | null;
  return row?.sweep_alias ?? null;
}

describe("readLegacyTimingRecord", () => {
  test("a pre-v2 dir whose samples.jsonl contains timing_round rows classifies v1-timing", async () => {
    const result = await readLegacyTimingRecord(join(FIXTURES_DIR, "legacy-timing-full"));
    expect(result.kind).toBe("v1-timing");
    if (result.kind !== "v1-timing") throw new Error("unreachable");
    expect(result.record.id).toBe("legacy-timing-full");
    expect(result.record.hfRepo).toBe("example-org/Example-LegacyTiming-GGUF");
  });

  test("only timing_round rows become metrics -- needle/quality rows never become trials", async () => {
    const result = await readLegacyTimingRecord(join(FIXTURES_DIR, "legacy-timing-full"));
    if (result.kind !== "v1-timing") throw new Error("unreachable");
    expect(result.record.trials).toHaveLength(0);
    expect(result.record.metrics).toHaveLength(2);
    expect(result.record.metrics.every((m) => m.metricType === "timing_round")).toBe(true);
    expect(result.record.metrics.map((m) => m.roundType).sort()).toEqual(["cold", "hot"]);
  });

  test("alias flows through from the top-level config.alias key (pre-v2 shape), never invented", async () => {
    const result = await readLegacyTimingRecord(join(FIXTURES_DIR, "legacy-timing-full"));
    if (result.kind !== "v1-timing") throw new Error("unreachable");
    expect(result.record.sweepAlias).toBe("fixture-legacy-timing-alias");
  });

  test("never carries prompts/completions or a wilson summary -- timing rows and only those", async () => {
    const result = await readLegacyTimingRecord(join(FIXTURES_DIR, "legacy-timing-full"));
    if (result.kind !== "v1-timing") throw new Error("unreachable");
    expect(result.record.rawPrompts).toEqual([]);
    expect(result.record.rawCompletions).toEqual([]);
    expect(result.record.wilsonSummary).toBeNull();
    expect(result.record.samplingArm).toBeNull();
    expect(result.record.battery).toBeNull();
    expect(result.record.vramProfile).toBeNull();
  });

  test("a pre-v2 dir without any timing_round rows classifies v1 (still fully skipped)", async () => {
    const result = await readLegacyTimingRecord(join(FIXTURES_DIR, "legacy-no-timing"));
    expect(result.kind).toBe("v1");
  });

  test("a pre-v2 dir with no samples.jsonl at all classifies v1, never invalid", async () => {
    const result = await readLegacyTimingRecord(join(LEGACY_FIXTURES_DIR, "fixture-v1-legacy"));
    expect(result.kind).toBe("v1");
  });

  test("a real v2 dir is not this door's business -- readLegacyTimingRecord defers to v1", async () => {
    const result = await readLegacyTimingRecord(join(FIXTURES_DIR, "v2-control"));
    expect(result.kind).toBe("v1");
  });

  test("selection is content-based, not a name-glob: this fixture dir name has no '-full'-glob significance", async () => {
    // legacy-no-timing has no timing_round rows and is skipped even though a name-glob
    // heuristic on its own would say nothing either way -- the real-world equivalent is
    // `qwen38-27b-iq3s-kv-e-alt-q4q4-c120000` / `wp1-dry-run-nemotron-4b`, two of the
    // twelve qualifying legacy dirs that do NOT end in `-full`.
    const result = await readLegacyTimingRecord(join(FIXTURES_DIR, "legacy-no-timing"));
    expect(result.kind).toBe("v1");
  });
});

describe("runIngest: legacy timing-only side door", () => {
  test("scans the fixture tree: 1 v2 ingested, 1 legacy-timing ingested, 1 v1 skipped, 0 invalid", async () => {
    const summary = await runIngest({ recordsDir: FIXTURES_DIR, rebuild: false });
    expect(summary.scanned).toBe(3);
    expect(summary.ingested).toBe(1);
    expect(summary.legacyTimingIngested).toBe(1);
    expect(summary.skippedV1).toBe(1);
    expect(summary.skippedInvalid).toBe(0);
  });

  test("legacy-timing-full is ingested under explicit v1-timing provenance", () => {
    expect(schemaOrigin("legacy-timing-full")).toBe("v1-timing");
  });

  test("v2-control keeps the default v2 provenance", () => {
    expect(schemaOrigin("v2-control")).toBe("v2");
  });

  test("legacy-no-timing never reaches the experiments table (fully skipped, not a v1-timing row)", () => {
    const ids = listExperimentSummaries().map((e) => e.id);
    expect(ids).not.toContain("legacy-no-timing");
    expect(schemaOrigin("legacy-no-timing")).toBeNull();
  });

  test("legacy-timing-full contributes only timing_round metrics, zero trials", () => {
    expect(listTrials("legacy-timing-full")).toHaveLength(0);
    const metrics = listMetrics("legacy-timing-full");
    expect(metrics).toHaveLength(2);
    expect(metrics.every((m) => m.metricType === "timing_round")).toBe(true);
  });

  test("legacy-timing-full's alias is stored on sweep_alias for downstream alias-joins (WP-3)", () => {
    expect(sweepAlias("legacy-timing-full")).toBe("fixture-legacy-timing-alias");
  });

  test("v2 ingest is byte-identical alongside the legacy side door: v2-control's trial/metric shape is untouched", () => {
    expect(listTrials("v2-control")).toHaveLength(1);
    expect(listMetrics("v2-control")).toHaveLength(0);
    expect(sweepAlias("v2-control")).toBe("fixture-v2-control-alias");
  });

  test("re-ingesting the same tree twice is idempotent: provenance and counts survive a second pass", async () => {
    const again = await runIngest({ recordsDir: FIXTURES_DIR, rebuild: false });
    expect(again.ingested).toBe(1);
    expect(again.legacyTimingIngested).toBe(1);
    expect(again.skippedV1).toBe(1);
    expect(schemaOrigin("legacy-timing-full")).toBe("v1-timing");
    expect(schemaOrigin("v2-control")).toBe("v2");
  });
});
