import { describe, expect, test } from "bun:test";
import type { ServingTimingRow } from "@bench-dashboard/shared";
import { formatServingDecodeCell, formatServingPrefillCell, matchServingTiming } from "./matchTiming.ts";

function timingRow(overrides: Partial<ServingTimingRow>): ServingTimingRow {
  return {
    modelKey: "fixture-model",
    aliases: ["fixture-alias"],
    p50PrefillTokS: 360.5,
    p90PrefillTokS: 390.2,
    p50DecodeTokS: 62.15,
    p90DecodeTokS: 65.4,
    samplingArm: "production",
    sampleCount: 10,
    mtp: false,
    ...overrides,
  };
}

describe("matchServingTiming", () => {
  test("matches when a timing row's modelKey equals the served model's modelKey", () => {
    const row = timingRow({ modelKey: "fixture-model" });
    const match = matchServingTiming("fixture-model", [row]);
    expect(match).toBe(row);
  });

  test("no match returns null, never a guessed row (the bge-* embed/rerank case)", () => {
    const row = timingRow({ modelKey: "some-other-model" });
    const match = matchServingTiming("fixture-model", [row]);
    expect(match).toBeNull();
  });

  test("an empty timing-rows array returns null", () => {
    expect(matchServingTiming("fixture-model", [])).toBeNull();
  });
});

describe("formatServingPrefillCell", () => {
  test("formats p50/p90 plus an (arm, n=) suffix", () => {
    const row = timingRow({ p50PrefillTokS: 360.5, p90PrefillTokS: 390.2, samplingArm: "production", sampleCount: 3 });
    expect(formatServingPrefillCell(row)).toBe("360.5 / 390.2 tok/s (production, n=3)");
  });

  test("null (no match) renders the literal em-dash", () => {
    expect(formatServingPrefillCell(null)).toBe("—");
  });

  test("a matched row with null prefill percentiles renders the em-dash (prefill samples may be fewer than decode)", () => {
    const row = timingRow({ p50PrefillTokS: null, p90PrefillTokS: null });
    expect(formatServingPrefillCell(row)).toBe("—");
  });

  test("an unlabelled arm (samplingArm: null) renders the literal 'unlabelled', never blank or 'null'", () => {
    const row = timingRow({ samplingArm: null, sampleCount: 1 });
    expect(formatServingPrefillCell(row)).toContain("(unlabelled, n=1)");
  });
});

describe("formatServingDecodeCell", () => {
  test("formats p50/p90 plus an (arm, n=) suffix", () => {
    const row = timingRow({ p50DecodeTokS: 62.15, p90DecodeTokS: 65.4, samplingArm: "capability", sampleCount: 3 });
    expect(formatServingDecodeCell(row)).toBe("62.1 / 65.4 tok/s (capability, n=3)");
  });

  test("null (no match) renders the literal em-dash, never a guessed pair", () => {
    expect(formatServingDecodeCell(null)).toBe("—");
  });

  test("a matched row with null decode percentiles (defensive: the aggregate never actually emits this) still renders honestly", () => {
    const row = timingRow({ p50DecodeTokS: null, p90DecodeTokS: null });
    expect(formatServingDecodeCell(row)).toBe("—");
  });
});
