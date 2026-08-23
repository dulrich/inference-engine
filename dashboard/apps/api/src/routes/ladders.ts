import type { Hono } from "hono";
import { listLadderRows } from "../db/experiments.ts";

// `GET /api/ladders/*` — `hfRepo` (e.g. `unsloth/gemma-4-12B-it-qat-GGUF`) contains a `/`,
// so this is a wildcard route rather than a Hono `:param` (which stops at the first `/`);
// the repo id is everything after `/api/ladders/`.
export function registerLadderRoutes(app: Hono): void {
  app.get("/api/ladders/*", (c) => {
    const hfRepo = decodeURIComponent(c.req.path.replace(/^\/api\/ladders\//, ""));
    if (hfRepo.length === 0) return c.json({ error: "hfRepo is required" }, 400);
    return c.json(listLadderRows(hfRepo));
  });
}
