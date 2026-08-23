import type { CurveSeriesPoint } from "@bench-dashboard/shared";
import { buildTaskAlignedChartData, type TaskAlignedChartData } from "./chartData.ts";

/** Shapes `GET /api/curves/:id`'s points (already grouped/Wilson-scored server-side, one
 * point per `(task, positionFraction)` cell -- `apps/api/src/routes/curves.ts`) into a
 * uPlot-ready aligned structure: one shared x axis (position fraction, 0..1), one failure-
 * rate series per task. */
export function buildCurveChartData(points: readonly CurveSeriesPoint[]): TaskAlignedChartData {
  return buildTaskAlignedChartData(points.map((p) => ({ task: p.task, x: p.positionFraction, y: p.failureRate })));
}
