import { describe, expect, test } from "bun:test";
import type { MetricRow } from "@bench-dashboard/shared";
import { computeVramFitRow } from "./vram.ts";

function metric(overrides: Partial<MetricRow>): MetricRow {
  return {
    id: 1,
    experimentId: "exp-1",
    metricType: "vram_baseline",
    roundType: null,
    roundIndex: null,
    bytes: null,
    firstGenerationTokenMs: null,
    observedDecodeTokS: null,
    promptTokens: null,
    cachedTokens: null,
    cacheHitRatio: null,
    cacheAssertionPassed: null,
    raw: null,
    ...overrides,
  };
}

describe("computeVramFitRow", () => {
  test("null when no vram_* metric rows are present", () => {
    expect(computeVramFitRow([metric({ metricType: "timing_round", bytes: null })])).toBeNull();
    expect(computeVramFitRow([])).toBeNull();
  });

  test("computes deltas from all three snapshots", () => {
    const row = computeVramFitRow([
      metric({ metricType: "vram_baseline", bytes: 100_000_000 }),
      metric({ metricType: "vram_loaded", bytes: 500_000_000 }),
      metric({ metricType: "vram_after_teardown", bytes: 100_000_000 }),
    ]);
    expect(row).toEqual({
      baselineBytes: 100_000_000,
      loadedBytes: 500_000_000,
      afterTeardownBytes: 100_000_000,
      loadDeltaBytes: 400_000_000,
      teardownGapBytes: 0,
    });
  });

  test("a partial snapshot still returns a row, missing fields null", () => {
    const row = computeVramFitRow([metric({ metricType: "vram_baseline", bytes: 100_000_000 })]);
    expect(row).toEqual({
      baselineBytes: 100_000_000,
      loadedBytes: null,
      afterTeardownBytes: null,
      loadDeltaBytes: null,
      teardownGapBytes: null,
    });
  });

  test("a non-zero teardown gap surfaces a leak", () => {
    const row = computeVramFitRow([
      metric({ metricType: "vram_baseline", bytes: 100_000_000 }),
      metric({ metricType: "vram_after_teardown", bytes: 150_000_000 }),
    ]);
    expect(row?.teardownGapBytes).toBe(50_000_000);
  });
});
