// Thin entry for the vendored webkit build (packages/webkit/build.ts; canonical: fleet-tools/webkit/).
//   bun build.ts          production build into dist/
//   bun build.ts --watch  rebuild on src/ change
//   bun build.ts --dev    watch + dev server (dist/ with SPA fallback, /api proxied)
import { createWebkit } from "../../packages/webkit/build.ts";
import { config } from "./build.config.ts";

export const kit = createWebkit(config);

const USAGE = "usage: bun build.ts [--watch | --dev]\n\n  (no flags)  production build into dist/\n  --watch     rebuild on src/ change\n  --dev       watch + dev server (dist/ with SPA fallback, /api proxied)\n  -h, --help  print this usage and exit 0\n";

if (import.meta.main) {
  if (process.argv.slice(2).some((a) => a === "--help" || a === "-h")) process.stdout.write(USAGE);
  else await kit.main();
}
