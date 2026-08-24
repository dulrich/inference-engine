import { describe, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  computeCellTimeStats,
  computeEtaForQueue,
  computeQueueEta,
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
