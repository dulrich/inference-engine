import { describe, expect, test } from "bun:test";
import { buildTaskAlignedChartData, taskSeriesColor } from "./chartData.ts";

describe("buildTaskAlignedChartData", () => {
  test("aligns two tasks onto a shared, sorted x axis", () => {
    const data = buildTaskAlignedChartData([
      { task: "single_needle", x: 0.45, y: 0.5 },
      { task: "single_needle", x: 0.1, y: 0 },
      { task: "multi_needle", x: 0.1, y: 0.25 },
    ]);
    expect(data.xs).toEqual([0.1, 0.45]);
    expect(data.tasks).toEqual(["multi_needle", "single_needle"]);
    // multi_needle: point at 0.1 only -> [0.25, null]
    expect(data.seriesValues[0]).toEqual([0.25, null]);
    // single_needle: points at both -> [0, 0.5]
    expect(data.seriesValues[1]).toEqual([0, 0.5]);
  });

  test("empty input yields empty structures", () => {
    const data = buildTaskAlignedChartData([]);
    expect(data.xs).toEqual([]);
    expect(data.tasks).toEqual([]);
    expect(data.seriesValues).toEqual([]);
  });

  test("a single task/point round-trips", () => {
    const data = buildTaskAlignedChartData([{ task: "single_needle", x: 0.1, y: 1 }]);
    expect(data.xs).toEqual([0.1]);
    expect(data.tasks).toEqual(["single_needle"]);
    expect(data.seriesValues).toEqual([[1]]);
  });
});

describe("taskSeriesColor", () => {
  function stubToken(name: string): string {
    return `TOKEN(${name})`;
  }

  test("cycles through viz-1..viz-5 by index", () => {
    expect(taskSeriesColor(0, stubToken)).toBe("TOKEN(--viz-1)");
    expect(taskSeriesColor(4, stubToken)).toBe("TOKEN(--viz-5)");
  });

  test("wraps around past 5 tasks", () => {
    expect(taskSeriesColor(5, stubToken)).toBe("TOKEN(--viz-1)");
    expect(taskSeriesColor(7, stubToken)).toBe("TOKEN(--viz-3)");
  });
});
