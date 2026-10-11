// Thin entry for the vendored webkit build (packages/webkit/build.ts; canonical: fleet-tools/webkit/).
//   bun build.ts          production build into dist/
//   bun build.ts --watch  rebuild on src/ change
//   bun build.ts --dev    watch + dev server (dist/ with SPA fallback, /api proxied)
import { createWebkit } from "../../packages/webkit/build.ts";
import { config } from "./build.config.ts";

export const kit = createWebkit(config);

if (import.meta.main) await kit.main();
