import { existsSync } from "node:fs";
import { join, normalize, sep } from "node:path";
import { Hono } from "hono";
import { config } from "./config.ts";
import { registerCurveRoutes } from "./routes/curves.ts";
import { registerExperimentRoutes } from "./routes/experiments.ts";
import { registerExportRoutes } from "./routes/export.ts";
import { registerHealthRoutes } from "./routes/health.ts";
import { registerLadderRoutes } from "./routes/ladders.ts";
import { registerQueueRoutes } from "./routes/queue.ts";
import { registerServingRoutes } from "./routes/serving.ts";

// Fleet scaffold convention (model_scoreboard `app.ts`): the Hono app object only — no
// `Bun.serve` call here, so importing this module (tests, `smoke.ts`) never binds a port
// or has any other side effect. `index.ts` is the only file that boots a real server.

const app = new Hono();

registerHealthRoutes(app);
registerExperimentRoutes(app);
registerLadderRoutes(app);
registerCurveRoutes(app);
registerServingRoutes(app);
registerExportRoutes(app);
registerQueueRoutes(app);

/** Serves the built SPA: any path inside the web root that exists on disk is returned as
 * a file, anything else falls back to `index.html` so the client-side router owns it.
 * Same resolved-path-containment discipline as the exemplar (`model_scoreboard app.ts`). */
function spaResponse(pathname: string): Response {
  const webRoot = normalize(config.webDir);
  const indexPath = join(webRoot, "index.html");
  const relativePath = pathname === "/" ? "index.html" : decodeURIComponent(pathname).replace(/^\/+/, "");
  const requestedPath = normalize(join(webRoot, relativePath));
  const isInsideWebRoot = requestedPath === webRoot || requestedPath.startsWith(webRoot + sep);
  const filePath = isInsideWebRoot && existsSync(requestedPath) ? requestedPath : indexPath;
  const isFallback = filePath === indexPath;
  const cacheControl = isFallback ? "no-cache" : "public, max-age=31536000, immutable";
  return new Response(Bun.file(filePath), { headers: { "Cache-Control": cacheControl } });
}

// GUARDED registration: with no built web dir (every `bun test` run, an API-only smoke
// process) the wildcard must not be registered at all, so an unknown `/api/*` path falls
// through to Hono's 404 instead of being served a nonexistent `index.html`.
if (existsSync(config.webDir)) {
  app.get("*", (c) => spaResponse(new URL(c.req.url).pathname));
}

export { app };
