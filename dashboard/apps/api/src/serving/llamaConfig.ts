import type { ServingAlias, ServingFlags, ServingResponse } from "@bench-dashboard/shared";

// `GET /api/serving`'s server-side reader (cycle-1 blocking finding B1 — the browser gets
// no executable path to `llama/config.yaml`; only this module ever opens the file, and it
// never writes it — Decisions locked "Read-only over serving config"). Uses Bun's built-in
// `Bun.YAML.parse` (no `js-yaml`/`yaml` dependency — `llama/config.yaml` has no real YAML
// anchors, only llama-swap's own `${macro}` string substitution, which this module expands
// itself before flag extraction).

interface LlamaConfigModel {
  name?: string;
  aliases?: string[];
  cmd?: string;
  ttl?: number;
  checkEndpoint?: string;
}
interface LlamaConfigDoc {
  macros?: Record<string, string>;
  models?: Record<string, LlamaConfigModel>;
}

/** One-pass `${macroName}` substitution. `llama/config.yaml`'s macros never reference
 * each other (verified against the live file — `llama_server_common`/
 * `llama_server_chat_common` are both flat strings), so a single pass is sufficient; a
 * macro this repo's config doesn't define is left as a literal `${...}` token rather than
 * silently dropped, which shows up (harmlessly) as an unmatched flag lookup below rather
 * than as a wrong flag value. */
function expandMacros(cmd: string, macros: Record<string, string>): string {
  let expanded = cmd;
  for (const [name, value] of Object.entries(macros)) {
    expanded = expanded.split(`\${${name}}`).join(value);
  }
  return expanded;
}

/** Pulls the fixed flag set the Ladders/Serving views care about (`-c`, `-ctk`, `-ctv`,
 * `-fa`, `-ngl`) out of a fully-expanded command string — a simple positional token scan,
 * not a general argv parser (this repo's `cmd:` blocks are space/newline-separated flag
 * pairs, never quoted values). A flag not present in the command resolves to `null`,
 * never a guessed default. */
function extractFlags(cmdText: string): ServingFlags {
  const tokens = cmdText.split(/\s+/).filter((t) => t.length > 0);
  const valueAfter = (flag: string): string | undefined => {
    const index = tokens.indexOf(flag);
    return index >= 0 ? tokens[index + 1] : undefined;
  };
  const toInt = (raw: string | undefined): number | null => {
    if (raw === undefined || !/^\d+$/.test(raw)) return null;
    return Number.parseInt(raw, 10);
  };
  return {
    contextLength: toInt(valueAfter("-c")),
    kvCacheTypeK: valueAfter("-ctk") ?? null,
    kvCacheTypeV: valueAfter("-ctv") ?? null,
    flashAttention: valueAfter("-fa") ?? null,
    gpuLayers: toInt(valueAfter("-ngl")),
  };
}

/** `configText` -> the served-alias list, filtered to models that actually publish at
 * least one alias (a `models:` entry with no `aliases:` is not reachable by any consumer
 * and is never a "served alias" — the brief's "filtered" pin). */
export function parseServedAliases(configText: string): ServingAlias[] {
  const doc = Bun.YAML.parse(configText) as LlamaConfigDoc;
  const macros = doc.macros ?? {};
  const models = doc.models ?? {};
  const aliases: ServingAlias[] = [];
  for (const [modelKey, model] of Object.entries(models)) {
    const modelAliases = model.aliases ?? [];
    if (modelAliases.length === 0) continue;
    aliases.push({
      modelKey,
      name: model.name ?? null,
      aliases: modelAliases,
      ttl: typeof model.ttl === "number" ? model.ttl : null,
      checkEndpoint: model.checkEndpoint ?? null,
      flags: extractFlags(expandMacros(model.cmd ?? "", macros)),
    });
  }
  return aliases;
}

export async function readServingConfig(path: string): Promise<ServingResponse> {
  const text = await Bun.file(path).text();
  return { configPath: path, models: parseServedAliases(text) };
}
