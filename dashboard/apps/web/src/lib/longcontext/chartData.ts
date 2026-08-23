// Shared task-aligned chart shaping for both Long-context charts (position curve + real-
// token bins) -- CC-17: chart series are categorical (one per task), mapped by series
// INDEX onto `--viz-1..--viz-5`, never by verdict/status hue. A task's index is stable
// (alphabetical) across both charts and across a surround-driven rebuild.

export interface TaskPoint {
  task: string;
  x: number;
  y: number;
}

export interface TaskAlignedChartData {
  /** Sorted unique x values across every task -- the shared uPlot x axis. */
  xs: number[];
  /** Sorted unique task names -- one value-series per entry, in this order. */
  tasks: string[];
  /** `seriesValues[i]` is task `tasks[i]`'s y values aligned to `xs`; `null` where that
   * task has no point at that x (a missing measurement, never a 0). */
  seriesValues: (number | null)[][];
}

export function buildTaskAlignedChartData(points: readonly TaskPoint[]): TaskAlignedChartData {
  const xs = [...new Set(points.map((p) => p.x))].sort((a, b) => a - b);
  const tasks = [...new Set(points.map((p) => p.task))].sort((a, b) => a.localeCompare(b));
  const seriesValues = tasks.map((task) =>
    xs.map((x) => points.find((p) => p.task === task && p.x === x)?.y ?? null),
  );
  return { xs, tasks, seriesValues };
}

/** `--viz-1`..`--viz-5` cycle by series index (CC-17) -- `token` is a caller-supplied CSS
 * custom-property reader (`getComputedStyle(document.documentElement).getPropertyValue`),
 * injected rather than read directly so this stays testable with a stub in a non-DOM
 * `bun:test` run. */
export function taskSeriesColor(index: number, token: (name: string) => string): string {
  const slot = (index % 5) + 1;
  return token(`--viz-${slot}`);
}
