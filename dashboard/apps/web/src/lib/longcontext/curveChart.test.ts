import { describe, expect, test } from "bun:test";
import type { CurveSeriesPoint } from "@bench-dashboard/shared";
import { buildCurveChartData } from "./curveChart.ts";

function point(overrides: Partial<CurveSeriesPoint>): CurveSeriesPoint {
  return {
    task: "single_needle",
    positionFraction: 0.1,
    n: 4,
    k: 1,
    failureRate: 0.25,
    sided: "two",
    confidence: 0.95,
    lower: 0.01,
    upper: 0.65,
    ...overrides,
  };
}

describe("buildCurveChartData", () => {
  test("uses positionFraction as x and failureRate as y", () => {
    const data = buildCurveChartData([
      point({ positionFraction: 0.1, failureRate: 0.25 }),
      point({ positionFraction: 0.45, failureRate: 0.5 }),
    ]);
    expect(data.xs).toEqual([0.1, 0.45]);
    expect(data.tasks).toEqual(["single_needle"]);
    expect(data.seriesValues).toEqual([[0.25, 0.5]]);
  });

  test("multiple tasks become multiple series", () => {
    const data = buildCurveChartData([
      point({ task: "single_needle", positionFraction: 0.1, failureRate: 0 }),
      point({ task: "multi_needle", positionFraction: 0.1, failureRate: 0.5 }),
    ]);
    expect(data.tasks).toEqual(["multi_needle", "single_needle"]);
    expect(data.seriesValues).toEqual([[0.5], [0]]);
  });

  test("empty points yield an empty chart", () => {
    const data = buildCurveChartData([]);
    expect(data.xs).toEqual([]);
    expect(data.tasks).toEqual([]);
  });
});
