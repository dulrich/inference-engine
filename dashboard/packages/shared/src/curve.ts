import { z } from "zod";

// `GET /api/curves/:experimentId` — a reliability-curve series: one point per
// (task, position_fraction) cell of `longcontext_probe` trials, with the per-cell Wilson
// interval. `sided`/`confidence` are carried on every point (never assumed from context —
// same convention `longcontext.wilson_interval` itself enforces).

export const CurveSeriesPointSchema = z.object({
  task: z.string(),
  positionFraction: z.number(),
  n: z.number().int().nonnegative(),
  k: z.number().int().nonnegative(),
  failureRate: z.number(),
  sided: z.enum(["one", "two"]),
  confidence: z.number(),
  lower: z.number().nullable(),
  upper: z.number().nullable(),
});
export type CurveSeriesPoint = z.infer<typeof CurveSeriesPointSchema>;

export const CurveSeriesSchema = z.object({
  experimentId: z.string(),
  points: z.array(CurveSeriesPointSchema),
});
export type CurveSeries = z.infer<typeof CurveSeriesSchema>;
