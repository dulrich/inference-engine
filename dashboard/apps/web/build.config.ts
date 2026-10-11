// App-specific constants for the vendored webkit build (packages/webkit/build.ts) — the only block an app edits.
import type { WebkitConfig } from "../../packages/webkit/build.ts";

// Dev port PINNED to 5810 — the SERVICES.md-reserved pair for "Inference Engine
// (dashboard)" (cycle-1 material finding M3: 5810 was documented but never wired). API on
// 4810 (`apps/api/src/config.ts` default `PORT`). Bun.serve has no port fallback, so the
// former vite `strictPort: true` is inherent: a taken port fails the dev server.
export const DEV_PORT = 5810;
export const DEV_HOST: string | undefined = undefined; // e.g. "0.0.0.0" for LAN access
export const PROXY: Record<string, string> = {
  "/api": "http://127.0.0.1:4810",
  "/health": "http://127.0.0.1:4810",
};
export const NAMING = {
  entry: "[name].[ext]",
  chunk: "assets/[name]-[hash].[ext]",
  asset: "assets/[name]-[hash].[ext]",
};
// Only these build-time env vars reach client code, as `import.meta.env.<KEY>`.
export const ENV_KEYS = ["VITE_COMMAND_CENTER_URL"] as const;

export const config: WebkitConfig = { here: import.meta.dir, DEV_PORT, DEV_HOST, PROXY, NAMING, ENV_KEYS };
