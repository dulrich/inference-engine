import { describe, expect, test } from "bun:test";
import type { QueueEta } from "@bench-dashboard/shared";
import { ETA_NO_DATA_NOTE, etaBasisLabel, formatMinutesAsHm, formatNextWindowEstimate, formatQueueEtaLine } from "./eta.ts";

function eta(overrides: Partial<QueueEta> = {}): QueueEta {
  return {
    remainingCells: 5,
    medianCellMinutes: 10,
    estimatedMinutesRemaining: 50,
    cellsPerHour: 6,
    basis: "current-run",
    sampleSize: 3,
    ...overrides,
  };
}

describe("formatMinutesAsHm", () => {
  test("null -> em-dash", () => {
    expect(formatMinutesAsHm(null)).toBe("—");
  });

  test("non-finite -> em-dash", () => {
    expect(formatMinutesAsHm(Number.NaN)).toBe("—");
  });

  test("splits whole minutes into H h M m", () => {
    expect(formatMinutesAsHm(970)).toBe("16 h 10 m");
  });

  test("rounds sub-minute precision before splitting", () => {
    expect(formatMinutesAsHm(970.4)).toBe("16 h 10 m");
    expect(formatMinutesAsHm(969.6)).toBe("16 h 10 m");
  });

  test("under an hour still shows the 0 h part", () => {
    expect(formatMinutesAsHm(45)).toBe("0 h 45 m");
  });

  test("zero minutes", () => {
    expect(formatMinutesAsHm(0)).toBe("0 h 0 m");
  });
});

describe("etaBasisLabel", () => {
  test("maps all known bases", () => {
    expect(etaBasisLabel("current-run")).toBe("current run");
    expect(etaBasisLabel("prior-runs")).toBe("prior runs");
    expect(etaBasisLabel("model-table")).toBe("model/context table");
  });

  test("null -> no data yet", () => {
    expect(etaBasisLabel(null)).toBe("no data yet");
  });
});

describe("formatQueueEtaLine", () => {
  test("null eta (error entry) -> the quiet not-enough-data note, never an error string", () => {
    expect(formatQueueEtaLine(null)).toBe(ETA_NO_DATA_NOTE);
  });

  test("basis null -> the quiet note even when other fields happen to be present", () => {
    expect(formatQueueEtaLine(eta({ basis: null, cellsPerHour: null, estimatedMinutesRemaining: null, medianCellMinutes: null, sampleSize: null }))).toBe(
      ETA_NO_DATA_NOTE,
    );
  });

  test("cellsPerHour null (e.g. medianCellMinutes 0) -> the quiet note", () => {
    expect(formatQueueEtaLine(eta({ cellsPerHour: null }))).toBe(ETA_NO_DATA_NOTE);
  });

  test("estimatedMinutesRemaining null (no sibling state file to size remainingCells) -> the quiet note", () => {
    expect(formatQueueEtaLine(eta({ estimatedMinutesRemaining: null, remainingCells: null }))).toBe(ETA_NO_DATA_NOTE);
  });

  test("full data renders the spec's example shape", () => {
    const line = formatQueueEtaLine(eta({ cellsPerHour: 3.2, estimatedMinutesRemaining: 970, basis: "current-run", sampleSize: 12 }));
    expect(line).toBe("~3.2 cells/hour · est. 16 h 10 m remaining (basis: current run, n=12)");
  });

  test("prior-runs basis renders its own label", () => {
    const line = formatQueueEtaLine(eta({ basis: "prior-runs", cellsPerHour: 5, estimatedMinutesRemaining: 30, sampleSize: 8 }));
    expect(line).toBe("~5.0 cells/hour · est. 0 h 30 m remaining (basis: prior runs, n=8)");
  });

  test("sampleSize null renders n=0 rather than n=null", () => {
    const line = formatQueueEtaLine(eta({ sampleSize: null }));
    expect(line).toContain("n=0");
  });

  test("model-table basis with scalingCaveat true appends the terse ±scaled marker", () => {
    const line = formatQueueEtaLine(eta({ basis: "model-table", scalingCaveat: true, cellsPerHour: 6, estimatedMinutesRemaining: 30, sampleSize: 1 }));
    expect(line).toBe("~6.0 cells/hour · est. 0 h 30 m remaining (basis: model/context table ±scaled, n=1)");
  });

  test("model-table basis with scalingCaveat false renders the plain label, no suffix", () => {
    const line = formatQueueEtaLine(eta({ basis: "model-table", scalingCaveat: false, cellsPerHour: 6, estimatedMinutesRemaining: 10, sampleSize: 1 }));
    expect(line).toBe("~6.0 cells/hour · est. 0 h 10 m remaining (basis: model/context table, n=1)");
  });

  test("a legacy basis is never suffixed even if scalingCaveat were somehow present", () => {
    const line = formatQueueEtaLine(eta({ basis: "current-run", scalingCaveat: undefined }));
    expect(line).not.toContain("±scaled");
  });
});

describe("formatNextWindowEstimate", () => {
  test("null eta (error entry) -> null, render nothing", () => {
    expect(formatNextWindowEstimate(null)).toBeNull();
    expect(formatNextWindowEstimate(undefined)).toBeNull();
  });

  test("cellsPerHour null -> null, never a fabricated 0", () => {
    expect(formatNextWindowEstimate(eta({ cellsPerHour: null }))).toBeNull();
  });

  test("zero cellsPerHour is real data, not missing data -> renders ≈0", () => {
    expect(formatNextWindowEstimate(eta({ cellsPerHour: 0 }))).toBe("≈0 cells in the next 60 min");
  });

  test("fractional cellsPerHour floors, never rounds up", () => {
    expect(formatNextWindowEstimate(eta({ cellsPerHour: 5.9 }))).toBe("≈5 cells in the next 60 min");
  });

  test("normal case at the default 60-minute window", () => {
    expect(formatNextWindowEstimate(eta({ cellsPerHour: 5 }))).toBe("≈5 cells in the next 60 min");
  });

  test("non-default window scales the count and the label", () => {
    expect(formatNextWindowEstimate(eta({ cellsPerHour: 6 }), 30)).toBe("≈3 cells in the next 30 min");
  });
});
