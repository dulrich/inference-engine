// `bun run check:recipes` -- the CLI over two orthogonal conformance policies, both run
// against `llama/config.yaml`'s served entries (`plans/bench-dashboard-serving-control.md`,
// WP-1 and WP-3):
//   - `servingRecipeConformance.ts` -- the capability-recipe policy: every served CHAT entry
//     against `docs/model-capabilities.md`'s required key set.
//   - `servingVisibilityConformance.ts` -- the visibility/gateway policy: EVERY served key
//     (embed/rerank included) against `router/visibility.json`'s declaration and the
//     `router/gateway.config.json` max-tokens crosswalk.
// Split by review finding F7 -- modularity: the two policies, their types and their
// diagnostics used to live in one module whose name described only the first. This file
// stays their one CLI entry point (one `bun run check:recipes`, one config parse) and
// aggregates their findings -- it does not become a generic validator registry.
//
// Why it lives in `dashboard/scripts/`: `Bun.YAML.parse` (used by
// `apps/api/src/serving/llamaConfig.ts`) is the only YAML parser available anywhere on this
// repo's implementation boundary without adding a dependency -- `llama/bakeoff` is stdlib-only
// by contract and `router/*.mjs` is plain Node over JSON. It reuses `parseServedAliases` rather
// than parsing the file a second time, so the gate and the dashboard can never disagree about
// what the config says. The alias list is parsed exactly ONCE here and handed to both checker
// modules -- they do no I/O of their own.
//
// Pure function of the config file plus its two companions: no network, no live service, no
// write path of any kind. The default config path is resolved from `import.meta.dir`, never
// from the cwd, so `bun run check:recipes` checks the repo's own file wherever it is invoked
// from.
import { join } from "node:path";
import { parseServedAliases } from "../apps/api/src/serving/llamaConfig.ts";
import { checkRecipeConformance, formatFinding, type RecipeFinding } from "./servingRecipeConformance.ts";
import { checkVisibilityConformance, resolveGatewayPath, resolveVisibilityPath } from "./servingVisibilityConformance.ts";

export { formatFinding, resolveGatewayPath, resolveVisibilityPath, type RecipeFinding };

/** `<repo>/llama/config.yaml`, resolved from this file's own location. */
export const DEFAULT_CONFIG_PATH = join(import.meta.dir, "../../llama/config.yaml");

/** Every non-conformance in `configText`, in config order: every recipe finding first, then
 * every visibility/gateway finding (matching the two checkers' own list-order guarantees --
 * see each module's header). An empty array means the file conforms to both policies. */
export function checkServingRecipes(
  configText: string,
  visibilityText: string | null = null,
  gatewayText: string | null = null,
): RecipeFinding[] {
  const aliases = parseServedAliases(configText);
  return [
    ...checkRecipeConformance(aliases),
    ...checkVisibilityConformance(aliases, visibilityText, gatewayText),
  ];
}

/** Reads the config plus its two companions. A companion that cannot be read is passed through
 * as `null` rather than throwing — the checker turns that into a finding. */
export async function checkServingRecipesFile(
  path: string,
  visibilityPath: string = resolveVisibilityPath(path),
  gatewayPath: string = resolveGatewayPath(path),
): Promise<RecipeFinding[]> {
  const readOrNull = async (file: string): Promise<string | null> => {
    try {
      return await Bun.file(file).text();
    } catch {
      return null;
    }
  };
  return checkServingRecipes(
    await Bun.file(path).text(),
    await readOrNull(visibilityPath),
    await readOrNull(gatewayPath),
  );
}

/** `--<name> <path>` (or `--<name>=<path>`), else `fallback`. */
export function parsePathArg(argv: string[], name: string, fallback: string): string {
  const index = argv.indexOf(`--${name}`);
  if (index >= 0 && argv[index + 1] !== undefined) return argv[index + 1] as string;
  const inline = argv.find((a) => a.startsWith(`--${name}=`));
  if (inline !== undefined) return inline.slice(`--${name}=`.length);
  return fallback;
}

/** `--config <path>` (or `--config=<path>`), defaulting to the repo's own `llama/config.yaml`. */
export function parseConfigArg(argv: string[]): string {
  return parsePathArg(argv, "config", DEFAULT_CONFIG_PATH);
}

if (import.meta.main) {
  const path = parseConfigArg(Bun.argv.slice(2));
  const findings = await checkServingRecipesFile(
    path,
    parsePathArg(Bun.argv.slice(2), "visibility", resolveVisibilityPath(path)),
    parsePathArg(Bun.argv.slice(2), "gateway", resolveGatewayPath(path)),
  );
  for (const finding of findings) console.log(formatFinding(finding));
  if (findings.length > 0) {
    console.log(`${findings.length} non-conforming entr${findings.length === 1 ? "y" : "ies"} in ${path}`);
    process.exit(1);
  }
  console.log(`ok — every served chat entry in ${path} conforms to docs/model-capabilities.md, and every served key is declared in router/visibility.json`);
}
