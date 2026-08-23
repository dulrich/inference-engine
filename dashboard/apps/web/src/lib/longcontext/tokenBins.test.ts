import { describe, expect, test } from "bun:test";
import { buildTokenBinChartData, groupByPromptTokens, type LongcontextTrialLike } from "./tokenBins.ts";

function trial(overrides: Partial<LongcontextTrialLike>): LongcontextTrialLike {
  return { task: "single_needle", promptTokens: 31000, classifierVerdict: "correct", ...overrides };
}

describe("groupByPromptTokens", () => {
  test("groups by (task, promptTokens), counting non-correct as failures", () => {
    const bins = groupByPromptTokens([
      trial({ promptTokens: 31000, classifierVerdict: "correct" }),
      trial({ promptTokens: 31000, classifierVerdict: "wrong" }),
      trial({ promptTokens: 31000, classifierVerdict: "degenerate" }),
    ]);
    expect(bins).toEqual([{ task: "single_needle", promptTokens: 31000, n: 3, k: 2, failureRate: 2 / 3 }]);
  });

  test("separates distinct promptTokens and tasks into distinct bins", () => {
    const bins = groupByPromptTokens([
      trial({ task: "single_needle", promptTokens: 10000, classifierVerdict: "correct" }),
      trial({ task: "single_needle", promptTokens: 31000, classifierVerdict: "wrong" }),
      trial({ task: "multi_needle", promptTokens: 10000, classifierVerdict: "correct" }),
    ]);
    expect(bins).toHaveLength(3);
    expect(bins[0]).toEqual({ task: "multi_needle", promptTokens: 10000, n: 1, k: 0, failureRate: 0 });
  });

  test("skips trials with a null task or promptTokens", () => {
    const bins = groupByPromptTokens([
      trial({ task: null }),
      trial({ promptTokens: null }),
      trial({ task: "single_needle", promptTokens: 5000 }),
    ]);
    expect(bins).toHaveLength(1);
  });

  test("sorts by task then promptTokens", () => {
    const bins = groupByPromptTokens([
      trial({ task: "single_needle", promptTokens: 31000 }),
      trial({ task: "multi_needle", promptTokens: 5000 }),
      trial({ task: "single_needle", promptTokens: 5000 }),
    ]);
    expect(bins.map((b) => `${b.task}:${b.promptTokens}`)).toEqual([
      "multi_needle:5000",
      "single_needle:5000",
      "single_needle:31000",
    ]);
  });

  test("empty input yields no bins", () => {
    expect(groupByPromptTokens([])).toEqual([]);
  });
});

describe("buildTokenBinChartData", () => {
  test("uses promptTokens as x and failureRate as y", () => {
    const data = buildTokenBinChartData([
      { task: "single_needle", promptTokens: 5000, n: 2, k: 0, failureRate: 0 },
      { task: "single_needle", promptTokens: 31000, n: 2, k: 1, failureRate: 0.5 },
    ]);
    expect(data.xs).toEqual([5000, 31000]);
    expect(data.seriesValues).toEqual([[0, 0.5]]);
  });
});
