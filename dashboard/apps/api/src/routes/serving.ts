import type { Hono } from "hono";
import { config } from "../config.ts";
import { readServingConfig } from "../serving/llamaConfig.ts";

// `GET /api/serving` — cycle-1 blocking finding B1: the Serving view's ONLY data source.
// Read-only over `LLAMA_CONFIG_PATH` (Decisions locked "Read-only over serving config" —
// this route never writes the file, and no other route touches it at all).
export function registerServingRoutes(app: Hono): void {
  app.get("/api/serving", async (c) => {
    try {
      const response = await readServingConfig(config.llamaConfigPath);
      return c.json(response);
    } catch (error) {
      return c.json({ error: error instanceof Error ? error.message : String(error) }, 502);
    }
  });
}
