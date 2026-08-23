import type { Hono } from "hono";
import { experimentCount } from "../db/experiments.ts";

// `GET /health` — probed BY BODY, never by status code (root `AGENTS.md` Quirks: "a
// healthy service has told you nothing"). Body shape is `{"status":"ok",...}` per this
// repo's standing convention (brief's File scope pin), not model_scoreboard's `{ok:true}`.
export function registerHealthRoutes(app: Hono): void {
  app.get("/health", (c) => c.json({ status: "ok", experimentCount: experimentCount() }));
}
