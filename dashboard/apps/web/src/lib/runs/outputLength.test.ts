import { describe, expect, test } from "bun:test";
import { flattenResponseCaps, summarizeCompletionTokens, summarizeFinishReasons } from "./outputLength.ts";

describe("summarizeCompletionTokens", () => {
  test("computes count/min/max/mean over non-null values", () => {
    const stats = summarizeCompletionTokens([
      { completionTokens: 10 },
      { completionTokens: 30 },
      { completionTokens: 20 },
    ]);
    expect(stats).toEqual({ count: 3, min: 10, max: 30, mean: 20 });
  });

  test("null completionTokens entries are excluded, not treated as zero", () => {
    const stats = summarizeCompletionTokens([{ completionTokens: 10 }, { completionTokens: null }]);
    expect(stats).toEqual({ count: 1, min: 10, max: 10, mean: 10 });
  });

  test("no trials with a completionTokens value -> null, not a zero-sample stats object", () => {
    expect(summarizeCompletionTokens([])).toBeNull();
    expect(summarizeCompletionTokens([{ completionTokens: null }])).toBeNull();
  });
});

describe("summarizeFinishReasons", () => {
  test("counts and sorts by descending count then reason name", () => {
    const rows = summarizeFinishReasons([
      { finishReason: "stop" },
      { finishReason: "length" },
      { finishReason: "stop" },
      { finishReason: "length" },
      { finishReason: "abort" },
      { finishReason: "stop" },
    ]);
    expect(rows).toEqual([
      { reason: "stop", count: 3 },
      { reason: "length", count: 2 },
      { reason: "abort", count: 1 },
    ]);
  });

  test("null finishReason entries contribute nothing", () => {
    expect(summarizeFinishReasons([{ finishReason: null }, { finishReason: null }])).toEqual([]);
  });

  test("no trials -> empty array", () => {
    expect(summarizeFinishReasons([])).toEqual([]);
  });
});

describe("flattenResponseCaps", () => {
  test("flattens WP-B's default + by_task shape into rows", () => {
    expect(
      flattenResponseCaps({ default: 256, by_task: { multi_needle: 768, single_needle: 256 } }),
    ).toEqual([
      { task: "default", cap: 256 },
      { task: "multi_needle", cap: 768 },
      { task: "single_needle", cap: 256 },
    ]);
  });

  test("null and empty objects produce no rows", () => {
    expect(flattenResponseCaps(null)).toEqual([]);
    expect(flattenResponseCaps({})).toEqual([]);
  });

  test("non-numeric values are skipped, never stringified", () => {
    expect(flattenResponseCaps({ default: "big", by_task: { a: "x", b: 5 }, note: { nested: 1 } })).toEqual([
      { task: "b", cap: 5 },
    ]);
  });
});
