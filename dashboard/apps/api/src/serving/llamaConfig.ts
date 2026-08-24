import type { ServingAlias, ServingFlagEntry, ServingFlags, ServingResponse } from "@bench-dashboard/shared";

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

// A token is a flag iff it starts with one or two dashes immediately followed by a letter
// (WP-1 spec, `plans/bench-dashboard-polish.md`) -- `-1`/`-0.8` fail this test (digit, not
// a letter, right after the dash(es)) and so read as VALUES, not flags; `${PORT}` and other
// non-dash tokens fail it too, same outcome.
const FLAG_TOKEN = /^--?[a-zA-Z]/;

/** Every `-`/`--` token from a fully-expanded command string, in order of appearance, paired
 * with the token immediately following it UNLESS that token is itself a flag by `FLAG_TOKEN`
 * (a boolean-style flag like `--jinja` then gets `value: null`) -- "show the full serving
 * config" (WP-1), so `-m <path>` is captured like any other flag. Tokens before the first
 * flag (the binary path) are excluded, since they're never a flag/value pair themselves. */
function extractAllFlags(tokens: string[]): ServingFlagEntry[] {
  const firstFlagIndex = tokens.findIndex((t) => FLAG_TOKEN.test(t));
  if (firstFlagIndex === -1) return [];
  const entries: ServingFlagEntry[] = [];
  let i = firstFlagIndex;
  while (i < tokens.length) {
    const flag = tokens[i] as string;
    if (!FLAG_TOKEN.test(flag)) {
      // Every non-flag token from `firstFlagIndex` on is consumed as some flag's value
      // below, so this only fires on a malformed/unparseable command; skip rather than
      // misattribute it as a flag.
      i += 1;
      continue;
    }
    const next = tokens[i + 1];
    if (next !== undefined && !FLAG_TOKEN.test(next)) {
      entries.push({ flag, value: next });
      i += 2;
    } else {
      entries.push({ flag, value: null });
      i += 1;
    }
  }
  return entries;
}

/** Pulls the fixed flag set the Ladders/Serving views care about (`-c`, `-ctk`, `-ctv`,
 * `-fa`, `-ngl`) out of a fully-expanded command string — a simple positional token scan,
 * not a general argv parser (this repo's `cmd:` blocks are space/newline-separated flag
 * pairs, never quoted values). A flag not present in the command resolves to `null`,
 * never a guessed default. `allFlags` (WP-1) is the additive full-config sibling of this
 * typed subset -- the subset itself is untouched. */
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
    allFlags: extractAllFlags(tokens),
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
