import { describe, expect, test } from "bun:test";
import type { ServingTimingRow } from "@bench-dashboard/shared";
import { formatServingTimingCell, matchServingTiming } from "./matchTiming.ts";

function timingRow(overrides: Partial<ServingTimingRow>): ServingTimingRow {
  return { alias: "fixture-alias", p50DecodeTokS: 62.15, sampleCount: 10, ...overrides };
}

describe("matchServingTiming", () => {
  test("matches when the served alias list contains the timing row's alias", () => {
    const row = timingRow({ alias: "fixture-alias" });
    const match = matchServingTiming(["fixture-alias"], [row]);
    expect(match).toBe(row);
  });

  test("matches if ANY of several served aliases matches (a tombstoned alias plus its replacement)", () => {
    const row = timingRow({ alias: "fixture-alias-new" });
    const match = matchServingTiming(["fixture-alias-old", "fixture-alias-new"], [row]);
    expect(match).toBe(row);
  });

  test("no match returns null, never a guessed row (the bge-* embed/rerank case)", () => {
    const row = timingRow({ alias: "some-other-alias" });
    const match = matchServingTiming(["fixture-alias"], [row]);
    expect(match).toBeNull();
  });

  test("an empty timing-rows array returns null", () => {
    expect(matchServingTiming(["fixture-alias"], [])).toBeNull();
  });
});

describe("formatServingTimingCell", () => {
  test("formats a match as tok/s plus an n= sample-count suffix", () => {
    expect(formatServingTimingCell(timingRow({ p50DecodeTokS: 62.15, sampleCount: 10 }))).toBe("62.1 tok/s (n=10)");
  });

  test("null (no match) renders the literal em-dash, never n=0", () => {
    expect(formatServingTimingCell(null)).toBe("—");
  });

  test("a matched row with a null p50 (defensive: the aggregate never actually emits this) still renders honestly", () => {
    expect(formatServingTimingCell(timingRow({ p50DecodeTokS: null, sampleCount: 1 }))).toBe("— (n=1)");
  });
});
