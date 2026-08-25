import { describe, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  computeCellTimeStats,
  computeEtaForQueue,
  computeQueueEta,
  readQueueCostSamples,
  readQueueRecordTimestamps,
  resolveBasis,
} from "./eta.ts";

// Fixtures are built per-test via `mkdtempSync` (the same convention `routes/queue.test.ts`
// uses for its `buildControlSection` scenarios) rather than a checked-in fixtures dir --
// the layout under test (`<recordsDir>/<queueStem>/<cell-dir>/config.json`) is small enough
// that inline construction keeps each test's exact shape visible at the call site.

function makeRecordsDir(prefix: string): string {
  return mkdtempSync(join(tmpdir(), `bench-dashboard-eta-${prefix}-`));
}

function writeCellConfig(recordsDir: string, queueStem: string, cellDirName: string, body: Record<string, unknown> | string): void {
  const cellDir = join(recordsDir, queueStem, cellDirName);
  mkdirSync(cellDir, { recursive: true });
  writeFileSync(join(cellDir, "config.json"), typeof body === "string" ? body : JSON.stringify(body));
}

/** Sibling `samples.jsonl` for a cost-table cell dir (WP-2 model/context-aware ETA,
 * `plans/bakeoff-timing-records-and-estimates.md`) -- one JSON object per line, the real
 * needle-probe wire shape. Assumes `writeCellConfig` already created the cell dir. */
function writeCellSamples(recordsDir: string, queueStem: string, cellDirName: string, rows: Record<string, unknown>[]): void {
  const cellDir = join(recordsDir, queueStem, cellDirName);
  mkdirSync(cellDir, { recursive: true });
  writeFileSync(join(cellDir, "samples.jsonl"), rows.map((row) => JSON.stringify(row)).join("\n"));
}

describe("readQueueRecordTimestamps", () => {
  test("queue stem dir that doesn't exist -> [], no throw", async () => {
    const recordsDir = makeRecordsDir("missing-queue-dir");
    expect(await readQueueRecordTimestamps(recordsDir, "no-such-queue")).toEqual([]);
  });

  test("queue stem dir exists but has no cell subdirectories -> []", async () => {
    const recordsDir = makeRecordsDir("empty-queue-dir");
    mkdirSync(join(recordsDir, "ladder-v1"), { recursive: true });
    expect(await readQueueRecordTimestamps(recordsDir, "ladder-v1")).toEqual([]);
  });

  test("collects the timestamp field from every cell dir's config.json", async () => {
    const recordsDir = makeRecordsDir("happy-path");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", { timestamp: "2026-08-24T20:00:00.000000+00:00" });
    writeCellConfig(recordsDir, "ladder-v1", "cell-b", { timestamp: "2026-08-24T20:10:00.000000+00:00" });
    const timestamps = await readQueueRecordTimestamps(recordsDir, "ladder-v1");
    expect(timestamps.sort()).toEqual(["2026-08-24T20:00:00.000000+00:00", "2026-08-24T20:10:00.000000+00:00"]);
  });

  test("a cell dir with no config.json at all is skipped, not fatal", async () => {
    const recordsDir = makeRecordsDir("no-config-json");
    mkdirSync(join(recordsDir, "ladder-v1", "cell-no-config"), { recursive: true });
    writeCellConfig(recordsDir, "ladder-v1", "cell-with-config", { timestamp: "2026-08-24T20:00:00Z" });
    const timestamps = await readQueueRecordTimestamps(recordsDir, "ladder-v1");
    expect(timestamps).toEqual(["2026-08-24T20:00:00Z"]);
  });

  test("invalid JSON in one cell's config.json is skipped, other cells still read", async () => {
    const recordsDir = makeRecordsDir("invalid-json");
    writeCellConfig(recordsDir, "ladder-v1", "cell-bad", "{not valid json");
    writeCellConfig(recordsDir, "ladder-v1", "cell-good", { timestamp: "2026-08-24T20:00:00Z" });
    const timestamps = await readQueueRecordTimestamps(recordsDir, "ladder-v1");
    expect(timestamps).toEqual(["2026-08-24T20:00:00Z"]);
  });

  test("a config.json missing the timestamp field is skipped", async () => {
    const recordsDir = makeRecordsDir("missing-timestamp");
    writeCellConfig(recordsDir, "ladder-v1", "cell-no-ts", { lineage: {} });
    const timestamps = await readQueueRecordTimestamps(recordsDir, "ladder-v1");
    expect(timestamps).toEqual([]);
  });

  test("a non-string timestamp field is skipped", async () => {
    const recordsDir = makeRecordsDir("non-string-timestamp");
    writeCellConfig(recordsDir, "ladder-v1", "cell-numeric-ts", { timestamp: 12345 });
    const timestamps = await readQueueRecordTimestamps(recordsDir, "ladder-v1");
    expect(timestamps).toEqual([]);
  });

  test("a stray file alongside cell directories is ignored (not treated as a cell dir)", async () => {
    const recordsDir = makeRecordsDir("stray-file");
    mkdirSync(join(recordsDir, "ladder-v1"), { recursive: true });
    writeFileSync(join(recordsDir, "ladder-v1", "README.md"), "not a cell dir");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", { timestamp: "2026-08-24T20:00:00Z" });
    const timestamps = await readQueueRecordTimestamps(recordsDir, "ladder-v1");
    expect(timestamps).toEqual(["2026-08-24T20:00:00Z"]);
  });
});

describe("computeCellTimeStats", () => {
  test("no timestamps -> everything null", () => {
    expect(computeCellTimeStats([])).toEqual({ medianCellMinutes: null, sampleSize: null, contributingTimestamps: [] });
  });

  test("a single timestamp has no deltas -> everything null", () => {
    expect(computeCellTimeStats(["2026-08-24T20:00:00Z"])).toEqual({
      medianCellMinutes: null,
      sampleSize: null,
      contributingTimestamps: [],
    });
  });

  test("retains a delta at exactly the 3600s boundary", () => {
    const stats = computeCellTimeStats(["2026-08-24T20:00:00Z", "2026-08-24T21:00:00Z"]);
    expect(stats.medianCellMinutes).toBe(60);
    expect(stats.sampleSize).toBe(1);
  });

  test("drops a delta just over the 3600s boundary -- between-window idle, not cell time", () => {
    const stats = computeCellTimeStats(["2026-08-24T20:00:00Z", "2026-08-24T21:00:01Z"]);
    expect(stats).toEqual({ medianCellMinutes: null, sampleSize: null, contributingTimestamps: [] });
  });

  test("a real ladder-style sequence: retained deltas median (odd count)", () => {
    // Three retained deltas: 5, 10, 15 minutes -> median 10.
    const stats = computeCellTimeStats([
      "2026-08-24T20:00:00Z",
      "2026-08-24T20:05:00Z",
      "2026-08-24T20:15:00Z",
      "2026-08-24T20:30:00Z",
    ]);
    expect(stats.medianCellMinutes).toBe(10);
    expect(stats.sampleSize).toBe(3);
    expect(stats.contributingTimestamps).toHaveLength(6); // both endpoints x 3 retained deltas
  });

  test("even count of retained deltas averages the two middle values", () => {
    // Two retained deltas: 5, 15 minutes -> median (5+15)/2 = 10.
    const stats = computeCellTimeStats(["2026-08-24T20:00:00Z", "2026-08-24T20:05:00Z", "2026-08-24T20:20:00Z"]);
    expect(stats.medianCellMinutes).toBe(10);
    expect(stats.sampleSize).toBe(2);
  });

  test("one huge between-window gap among several small deltas: only the small ones count", () => {
    const stats = computeCellTimeStats([
      "2026-08-23T09:00:00Z", // prior night
      "2026-08-23T09:05:00Z", // +5m, retained
      "2026-08-24T20:00:00Z", // +many hours, dropped (between-window idle)
      "2026-08-24T20:10:00Z", // +10m, retained
    ]);
    expect(stats.sampleSize).toBe(2);
    expect(stats.medianCellMinutes).toBe(7.5);
  });

  test("unparseable timestamp strings are dropped before sorting/delta computation", () => {
    const stats = computeCellTimeStats(["not-a-date", "2026-08-24T20:00:00Z", "2026-08-24T20:05:00Z"]);
    expect(stats.sampleSize).toBe(1);
    expect(stats.medianCellMinutes).toBe(5);
  });

  test("sorts out-of-order input before computing deltas", () => {
    const stats = computeCellTimeStats(["2026-08-24T20:10:00Z", "2026-08-24T20:00:00Z", "2026-08-24T20:05:00Z"]);
    expect(stats.sampleSize).toBe(2);
    expect(stats.medianCellMinutes).toBe(5);
  });
});

describe("resolveBasis", () => {
  test("no contributing timestamps -> null (no data at all)", () => {
    expect(resolveBasis([], null)).toBeNull();
    expect(resolveBasis([], "2026-08-24T20:00:00Z")).toBeNull();
  });

  test("liveStartedAt null (this queue is not the live one) -> prior-runs", () => {
    expect(resolveBasis(["2026-08-24T20:00:00Z"], null)).toBe("prior-runs");
  });

  test("a contributing timestamp at or after the live window's startedAt -> current-run", () => {
    expect(resolveBasis(["2026-08-24T20:05:00Z"], "2026-08-24T20:00:00Z")).toBe("current-run");
    expect(resolveBasis(["2026-08-24T20:00:00Z"], "2026-08-24T20:00:00Z")).toBe("current-run"); // exact equality counts
  });

  test("every contributing timestamp before the live window's startedAt -> prior-runs", () => {
    expect(resolveBasis(["2026-08-23T20:00:00Z"], "2026-08-24T20:00:00Z")).toBe("prior-runs");
  });

  test("an unparseable liveStartedAt falls back to prior-runs rather than throwing", () => {
    expect(resolveBasis(["2026-08-24T20:00:00Z"], "not-a-date")).toBe("prior-runs");
  });
});

describe("computeQueueEta", () => {
  test("no timestamps at all, but a known remainingCells -- remainingCells still populated, everything timing-related null", () => {
    const eta = computeQueueEta({ timestamps: [], remainingCells: 7, liveStartedAt: null });
    expect(eta).toEqual({
      remainingCells: 7,
      medianCellMinutes: null,
      estimatedMinutesRemaining: null,
      cellsPerHour: null,
      basis: null,
      sampleSize: null,
    });
  });

  test("remainingCells null (no sibling state file) alongside real timing data -- estimatedMinutesRemaining stays null, cellsPerHour still computed", () => {
    const eta = computeQueueEta({
      timestamps: ["2026-08-24T20:00:00Z", "2026-08-24T20:10:00Z"],
      remainingCells: null,
      liveStartedAt: null,
    });
    expect(eta.remainingCells).toBeNull();
    expect(eta.medianCellMinutes).toBe(10);
    expect(eta.cellsPerHour).toBe(6);
    expect(eta.estimatedMinutesRemaining).toBeNull();
  });

  test("full happy path: current-run basis, rounded estimate", () => {
    const eta = computeQueueEta({
      timestamps: ["2026-08-24T20:00:00Z", "2026-08-24T20:10:00Z", "2026-08-24T20:20:00Z"],
      remainingCells: 5,
      liveStartedAt: "2026-08-24T19:00:00Z",
    });
    expect(eta.medianCellMinutes).toBe(10);
    expect(eta.sampleSize).toBe(2);
    expect(eta.cellsPerHour).toBe(6);
    expect(eta.estimatedMinutesRemaining).toBe(50); // 5 cells x 10 min
    expect(eta.basis).toBe("current-run");
    expect(eta.remainingCells).toBe(5);
  });

  test("prior-runs basis when the live window started after every contributing record", () => {
    const eta = computeQueueEta({
      timestamps: ["2026-08-23T09:00:00Z", "2026-08-23T09:10:00Z"],
      remainingCells: 3,
      liveStartedAt: "2026-08-24T19:00:00Z",
    });
    expect(eta.basis).toBe("prior-runs");
  });

  test("a median of exactly zero minutes never produces an Infinity cellsPerHour", () => {
    const eta = computeQueueEta({
      timestamps: ["2026-08-24T20:00:00.000Z", "2026-08-24T20:00:00.000Z"],
      remainingCells: 4,
      liveStartedAt: null,
    });
    expect(eta.medianCellMinutes).toBe(0);
    expect(eta.cellsPerHour).toBeNull();
    expect(eta.estimatedMinutesRemaining).toBe(0);
  });

  test("results are rounded to one decimal place", () => {
    // Delta of 7 minutes -> cellsPerHour = 60/7 = 8.5714... -> rounds to 8.6.
    const eta = computeQueueEta({
      timestamps: ["2026-08-24T20:00:00Z", "2026-08-24T20:07:00Z"],
      remainingCells: 3,
      liveStartedAt: null,
    });
    expect(eta.cellsPerHour).toBe(8.6);
    expect(eta.estimatedMinutesRemaining).toBe(21); // 3 x 7, already whole
  });
});

describe("computeEtaForQueue", () => {
  test("recordsDir null -> no fs read attempted, timing fields null, remainingCells passed through", async () => {
    const eta = await computeEtaForQueue({ recordsDir: null, queueStem: "ladder-v1", remainingCells: 9, liveStartedAt: null });
    expect(eta).toEqual({
      remainingCells: 9,
      medianCellMinutes: null,
      estimatedMinutesRemaining: null,
      cellsPerHour: null,
      basis: null,
      sampleSize: null,
    });
  });

  test("reads real cell dirs under recordsDir/queueStem and folds them through computeQueueEta", async () => {
    const recordsDir = makeRecordsDir("integration");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a-e4fe5b21", { timestamp: "2026-08-24T20:00:00Z" });
    writeCellConfig(recordsDir, "ladder-v1", "cell-b-f00dcafe", { timestamp: "2026-08-24T20:10:00Z" });

    const eta = await computeEtaForQueue({
      recordsDir,
      queueStem: "ladder-v1",
      remainingCells: 2,
      liveStartedAt: "2026-08-24T19:55:00Z",
    });
    expect(eta.medianCellMinutes).toBe(10);
    expect(eta.sampleSize).toBe(1);
    expect(eta.cellsPerHour).toBe(6);
    expect(eta.estimatedMinutesRemaining).toBe(20);
    expect(eta.basis).toBe("current-run");
  });

  test("a different queue stem under the same recordsDir sees only its own records", async () => {
    const recordsDir = makeRecordsDir("cross-queue-isolation");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", { timestamp: "2026-08-24T20:00:00Z" });
    writeCellConfig(recordsDir, "ladder-v1", "cell-b", { timestamp: "2026-08-24T20:10:00Z" });

    const eta = await computeEtaForQueue({ recordsDir, queueStem: "other-queue", remainingCells: 1, liveStartedAt: null });
    expect(eta.basis).toBeNull();
    expect(eta.sampleSize).toBeNull();
  });
});

// ---- Model/context-aware cost table (WP-2, `plans/bakeoff-timing-records-and-estimates.md`)
// -- `readQueueCostSamples` directly, then the enriched-vs-legacy-vs-mixed behavior through
// `computeEtaForQueue`'s new `cellDescriptors` input. Every existing describe block above
// this comment is untouched by this plan.

describe("readQueueCostSamples", () => {
  test("queue stem dir that doesn't exist -> [], no throw", async () => {
    const recordsDir = makeRecordsDir("cost-missing-dir");
    expect(await readQueueCostSamples(recordsDir, "no-such-queue")).toEqual([]);
  });

  test("a record shaped like every record on disk today (no sweep_cell.model) contributes nothing", async () => {
    const recordsDir = makeRecordsDir("cost-no-model");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", {
      timestamp: "2026-08-24T20:00:00Z",
      cell_wall_seconds: 600,
      sweep_cell: { server_flags: { "-c": 32768 } },
    });
    expect(await readQueueCostSamples(recordsDir, "ladder-v1")).toEqual([]);
  });

  test("a record with a model but no numeric -c server flag contributes nothing", async () => {
    const recordsDir = makeRecordsDir("cost-no-context");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", {
      cell_wall_seconds: 600,
      sweep_cell: { model: "modelA", server_flags: {} },
    });
    expect(await readQueueCostSamples(recordsDir, "ladder-v1")).toEqual([]);
  });

  test("cell_wall_seconds is used directly as cost when present", async () => {
    const recordsDir = makeRecordsDir("cost-wall-seconds");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", {
      cell_wall_seconds: 600,
      cell_started_at: "2026-08-24T20:00:00Z",
      cell_ended_at: "2026-08-24T20:30:00Z", // deliberately disagrees -- wall_seconds wins
      sweep_cell: { model: "modelA", server_flags: { "-c": 32768 } },
    });
    const samples = await readQueueCostSamples(recordsDir, "ladder-v1");
    expect(samples).toEqual([{ model: "modelA", context: 32768, costSeconds: 600, avgPromptTokens: null }]);
  });

  test("cell_started_at/cell_ended_at diff is used when cell_wall_seconds is absent", async () => {
    const recordsDir = makeRecordsDir("cost-timestamp-diff");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", {
      cell_started_at: "2026-08-24T20:00:00Z",
      cell_ended_at: "2026-08-24T20:05:00Z",
      sweep_cell: { model: "modelA", server_flags: { "-c": 32768 } },
    });
    const samples = await readQueueCostSamples(recordsDir, "ladder-v1");
    expect(samples).toEqual([{ model: "modelA", context: 32768, costSeconds: 300, avgPromptTokens: null }]);
  });

  test("falls back to summed probe wall_seconds when neither cell_wall_seconds nor both timestamps are present", async () => {
    const recordsDir = makeRecordsDir("cost-probe-sum");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", {
      sweep_cell: { model: "modelA", server_flags: { "-c": 32768 } },
    });
    writeCellSamples(recordsDir, "ladder-v1", "cell-a", [
      { type: "longcontext_probe", wall_seconds: 20, prompt_tokens: 29000 },
      { type: "longcontext_probe", wall_seconds: 22, prompt_tokens: 31000 },
      { type: "some_other_row", wall_seconds: 999, prompt_tokens: 999 }, // never counted
    ]);
    const samples = await readQueueCostSamples(recordsDir, "ladder-v1");
    expect(samples).toEqual([{ model: "modelA", context: 32768, costSeconds: 42, avgPromptTokens: 30000 }]);
  });

  test("a cell with model+context identity but no cost basis at all is skipped entirely", async () => {
    const recordsDir = makeRecordsDir("cost-no-basis");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", {
      sweep_cell: { model: "modelA", server_flags: { "-c": 32768 } },
    });
    // No samples.jsonl at all -- summedWallSeconds is null too, so there is no basis.
    expect(await readQueueCostSamples(recordsDir, "ladder-v1")).toEqual([]);
  });

  test("avgPromptTokens ignores non-probe rows and non-positive prompt_tokens", async () => {
    const recordsDir = makeRecordsDir("cost-avg-prompt-tokens");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", {
      cell_wall_seconds: 600,
      sweep_cell: { model: "modelA", server_flags: { "-c": 32768 } },
    });
    writeCellSamples(recordsDir, "ladder-v1", "cell-a", [
      { type: "longcontext_probe", wall_seconds: 10, prompt_tokens: 100 },
      { type: "longcontext_probe", wall_seconds: 10, prompt_tokens: 300 },
      { type: "longcontext_probe", wall_seconds: 10, prompt_tokens: 0 }, // excluded, not positive
      { type: "timing_round", wall_seconds: 10, prompt_tokens: 999999 }, // excluded, wrong row type
    ]);
    const samples = await readQueueCostSamples(recordsDir, "ladder-v1");
    expect(samples).toEqual([{ model: "modelA", context: 32768, costSeconds: 600, avgPromptTokens: 200 }]);
  });

  test("multiple cell dirs each contribute their own sample", async () => {
    const recordsDir = makeRecordsDir("cost-multiple-cells");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", {
      cell_wall_seconds: 600,
      sweep_cell: { model: "modelA", server_flags: { "-c": 32768 } },
    });
    writeCellConfig(recordsDir, "ladder-v1", "cell-b", {
      cell_wall_seconds: 1200,
      sweep_cell: { model: "modelA", server_flags: { "-c": 65536 } },
    });
    const samples = await readQueueCostSamples(recordsDir, "ladder-v1");
    expect(samples.sort((a, b) => a.context - b.context)).toEqual([
      { model: "modelA", context: 32768, costSeconds: 600, avgPromptTokens: null },
      { model: "modelA", context: 65536, costSeconds: 1200, avgPromptTokens: null },
    ]);
  });
});

describe("computeEtaForQueue: model/context-aware fallback chain (WP-2)", () => {
  test("legacy-only records (today's real shape, no sweep_cell.model): byte-identical to the plain global median -- no bucketSampleSize/scalingCaveat keys at all", async () => {
    const recordsDir = makeRecordsDir("fallback-legacy-only");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", { timestamp: "2026-08-24T20:00:00Z" });
    writeCellConfig(recordsDir, "ladder-v1", "cell-b", { timestamp: "2026-08-24T20:10:00Z" });

    const input = { recordsDir, queueStem: "ladder-v1", remainingCells: 2, liveStartedAt: "2026-08-24T19:55:00Z" };
    const legacy = computeQueueEta({
      timestamps: await readQueueRecordTimestamps(recordsDir, "ladder-v1"),
      remainingCells: input.remainingCells,
      liveStartedAt: input.liveStartedAt,
    });

    const withoutDescriptors = await computeEtaForQueue(input);
    expect(withoutDescriptors).toEqual(legacy);
    expect(Object.keys(withoutDescriptors)).not.toContain("bucketSampleSize");
    expect(Object.keys(withoutDescriptors)).not.toContain("scalingCaveat");

    const withDescriptors = await computeEtaForQueue({
      ...input,
      cellDescriptors: [{ alias: "cell-c", model: "gemma-4-12b-qat", c: 32768, state: "pending" }],
    });
    expect(withDescriptors).toEqual(legacy);
    expect(Object.keys(withDescriptors)).not.toContain("bucketSampleSize");
    expect(Object.keys(withDescriptors)).not.toContain("scalingCaveat");
  });

  test("empty cellDescriptors array behaves exactly like omitting the field", async () => {
    const recordsDir = makeRecordsDir("fallback-empty-descriptors");
    writeCellConfig(recordsDir, "ladder-v1", "cell-a", { timestamp: "2026-08-24T20:00:00Z" });
    writeCellConfig(recordsDir, "ladder-v1", "cell-b", { timestamp: "2026-08-24T20:10:00Z" });

    const input = { recordsDir, queueStem: "ladder-v1", remainingCells: 2, liveStartedAt: null };
    const omitted = await computeEtaForQueue(input);
    const empty = await computeEtaForQueue({ ...input, cellDescriptors: [] });
    expect(empty).toEqual(omitted);
  });

  test("exact (model, context) match: median cost of the matching bucket, purely-exact basis", async () => {
    const recordsDir = makeRecordsDir("fallback-exact");
    writeCellConfig(recordsDir, "ladder-v1", "hist-a", {
      timestamp: "2026-08-24T20:00:00Z",
      cell_wall_seconds: 600, // 10 minutes
      sweep_cell: { model: "modelA", server_flags: { "-c": 32768 } },
    });

    const eta = await computeEtaForQueue({
      recordsDir,
      queueStem: "ladder-v1",
      remainingCells: 1,
      liveStartedAt: null,
      cellDescriptors: [{ alias: "cell-b", model: "modelA", c: 32768, state: "pending" }],
    });

    expect(eta.basis).toBe("model-table");
    expect(eta.medianCellMinutes).toBe(10);
    expect(eta.estimatedMinutesRemaining).toBe(10);
    expect(eta.cellsPerHour).toBe(6);
    expect(eta.bucketSampleSize).toBe(1);
    expect(eta.scalingCaveat).toBe(false);
  });

  test("same-model any-context scaled by prompt-token ratio: flags the caveat, projects across contexts", async () => {
    const recordsDir = makeRecordsDir("fallback-scaled");
    writeCellConfig(recordsDir, "ladder-v1", "hist-a", {
      cell_wall_seconds: 3600, // 60 minutes at c=32768
      sweep_cell: { model: "modelB", server_flags: { "-c": 32768 } },
    });
    writeCellSamples(recordsDir, "ladder-v1", "hist-a", [
      { type: "longcontext_probe", wall_seconds: 1800, prompt_tokens: 30000 },
      { type: "longcontext_probe", wall_seconds: 1800, prompt_tokens: 30000 },
    ]);

    const eta = await computeEtaForQueue({
      recordsDir,
      queueStem: "ladder-v1",
      remainingCells: 1,
      liveStartedAt: null,
      cellDescriptors: [{ alias: "cell-c", model: "modelB", c: 65536, state: "running" }],
    });

    // rate = 3600s / 30000 tokens = 0.12 s/token; projected = 0.12 * 65536 = 7864.32s = 131.072min.
    expect(eta.basis).toBe("model-table");
    expect(eta.medianCellMinutes).toBe(131.1);
    expect(eta.estimatedMinutesRemaining).toBe(131.1);
    expect(eta.cellsPerHour).toBe(0.5);
    expect(eta.bucketSampleSize).toBe(1);
    expect(eta.scalingCaveat).toBe(true);
  });

  test("mixed: one exact-basis cell + one cell with zero model data falling to the legacy global median", async () => {
    const recordsDir = makeRecordsDir("fallback-mixed");
    // Feeds BOTH the cost table (model identity) and the legacy timestamp scan (5-minute
    // retained delta -> legacy medianCellMinutes = 5).
    writeCellConfig(recordsDir, "ladder-v1", "hist-a", {
      timestamp: "2026-08-24T20:00:00Z",
      cell_wall_seconds: 600, // 10 minutes, modelA @ 32768 exact bucket
      sweep_cell: { model: "modelA", server_flags: { "-c": 32768 } },
    });
    // Legacy-shaped sibling (no sweep_cell.model) -- only feeds the timestamp scan.
    writeCellConfig(recordsDir, "ladder-v1", "hist-b", { timestamp: "2026-08-24T20:05:00Z" });

    const eta = await computeEtaForQueue({
      recordsDir,
      queueStem: "ladder-v1",
      remainingCells: 2,
      liveStartedAt: null,
      cellDescriptors: [
        { alias: "cell-x", model: "modelA", c: 32768, state: "pending" },
        { alias: "cell-y", model: "modelC", c: 99999, state: "pending" }, // no data anywhere for modelC
      ],
    });

    expect(eta.basis).toBe("model-table");
    // resolved per-cell minutes: [10 (exact), 5 (legacy fallback)] -> median 7.5, sum 15.
    expect(eta.medianCellMinutes).toBe(7.5);
    expect(eta.estimatedMinutesRemaining).toBe(15);
    expect(eta.cellsPerHour).toBe(8);
    expect(eta.bucketSampleSize).toBe(1); // only the exact-tier cell consumes a cost sample
    expect(eta.scalingCaveat).toBe(true); // not purely exact -- one cell used the legacy fallback
  });

  test("completed/failed descriptors never enter the remaining-cell sum", async () => {
    const recordsDir = makeRecordsDir("fallback-non-remaining-states");
    writeCellConfig(recordsDir, "ladder-v1", "hist-a", {
      cell_wall_seconds: 600,
      sweep_cell: { model: "modelA", server_flags: { "-c": 32768 } },
    });

    const eta = await computeEtaForQueue({
      recordsDir,
      queueStem: "ladder-v1",
      remainingCells: 0,
      liveStartedAt: null,
      cellDescriptors: [
        { alias: "cell-done", model: "modelA", c: 32768, state: "completed" },
        { alias: "cell-failed", model: "modelA", c: 32768, state: "failed" },
      ],
    });

    // No pending/running descriptors at all -- the cost table is never even consulted for
    // an estimate, so this is the same byte-identical legacy path as the empty-descriptors
    // case above (remainingCells: 0, no timestamps -> everything timing-related null).
    expect(eta.basis).toBeNull();
    expect(Object.keys(eta)).not.toContain("bucketSampleSize");
  });
});
