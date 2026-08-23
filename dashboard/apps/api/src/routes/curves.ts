import type { CurveSeriesPoint } from "@bench-dashboard/shared";
import type { Hono } from "hono";
import { wilsonInterval } from "../analysis/wilson.ts";
import { listLongcontextTrials } from "../db/experiments.ts";

// `GET /api/curves/:experimentId` — reliability-curve series: one point per
// `(task, position_fraction)` cell of that experiment's `longcontext_probe` trials
// (`llama/bakeoff/README.md` "Position sweep + statistics" — tasks/positions are NEVER
// averaged or merged, so this groups rather than rolling up). A cell's failure rate
// counts every non-`"correct"` `classifier_verdict` as a failure (wrong + degenerate +
// truncated(benign) all count — that module's own documented convention). This route's
// own call: two-sided 95% (the interval this dashboard reports by default; WP-2 chooses
// what to render).
export function registerCurveRoutes(app: Hono): void {
  app.get("/api/curves/:experimentId", (c) => {
    const experimentId = c.req.param("experimentId");
    const trials = listLongcontextTrials(experimentId);

    const groups = new Map<string, { task: string; positionFraction: number; n: number; k: number }>();
    for (const trial of trials) {
      if (trial.task === null || trial.position_fraction === null) continue;
      const key = `${trial.task}::${trial.position_fraction}`;
      const group = groups.get(key) ?? { task: trial.task, positionFraction: trial.position_fraction, n: 0, k: 0 };
      group.n += 1;
      if (trial.classifier_verdict !== "correct") group.k += 1;
      groups.set(key, group);
    }

    const points: CurveSeriesPoint[] = [...groups.values()]
      .sort((a, b) => a.task.localeCompare(b.task) || a.positionFraction - b.positionFraction)
      .map((group) => {
        const interval = wilsonInterval(group.k, group.n, "two", 0.95);
        return {
          task: group.task,
          positionFraction: group.positionFraction,
          n: group.n,
          k: group.k,
          failureRate: group.k / group.n,
          sided: interval.sided,
          confidence: interval.confidence,
          lower: interval.lower,
          upper: interval.upper,
        };
      });

    return c.json({ experimentId, points });
  });
}
