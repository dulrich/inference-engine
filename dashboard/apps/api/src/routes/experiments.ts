import type { Hono } from "hono";
import { getExperimentDetail, listExperimentSummaries, listMetrics, listTrials } from "../db/experiments.ts";

export function registerExperimentRoutes(app: Hono): void {
  app.get("/api/experiments", (c) => c.json(listExperimentSummaries()));

  app.get("/api/experiments/:id", (c) => {
    const id = c.req.param("id");
    const detail = getExperimentDetail(id);
    if (!detail) return c.json({ error: "not found" }, 404);
    return c.json({
      ...detail,
      trials: listTrials(id),
      metrics: listMetrics(id),
    });
  });
}
