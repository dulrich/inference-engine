import type {
  ServingAlias,
  ServingCapability,
  ServingFlagEntry,
  ServingFlags,
  ServingRecipe,
  ServingResponse,
  ServingSource,
} from "@bench-dashboard/shared";
import { projectRecipe } from "./recipe.ts";

// `GET /api/serving`'s server-side reader (cycle-1 blocking finding B1 — the browser gets
// no executable path to `llama/config.yaml`; only this module ever opens the file, and it
// never writes it — Decisions locked "Read-only over serving config"). Uses Bun's built-in
// `Bun.YAML.parse` (no `js-yaml`/`yaml` dependency — `llama/config.yaml` has no real YAML
// anchors, only llama-swap's own `${macro}` string substitution, which this module expands
// itself before flag extraction).
//
// WP-2 data-path decision (`plans/capability-surfacing-and-local-agent-endpoints.md` WP-2
// brief): capability metadata is read from THIS SAME bind-mounted `config.yaml`'s per-model
// `metadata:` block, never fetched live from the router's `GET /v1/models`. Two findings
// ruled the live-fetch alternative out: (1) the brief's literal condition -- "can the API
// container already reach :4811" -- is false by this fleet's own documented network fact
// (`fleet-tools/compose.fleet.yml`'s Open WebUI comment block, verified live
// 2026-08-20): :4811 is `127.0.0.1`-bound on the HOST, and a socket bound to loopback never
// accepts packets arriving from another container's bridge interface -- only the container
// DNS name (`inference-engine-llama:4806`, reachable over the shared default compose
// network) works, and reaching THAT would still be a new architectural dependency, not
// something already wired for this container. (2) this repo's own locked decision (`plans/
// bench-results-db-and-dashboard.md`, "Read-only over serving config") already commits the
// API to reading `llama/config.yaml` directly rather than calling the live service, and
// llama-swap surfaces `metadata:` into `/v1/models` verbatim (`docs/model-capabilities.md`)
// -- so the config file is the identical normative source with none of the new runtime
// dependency, container-restart ordering, or fixture-untestability a live call would add.

interface LlamaConfigCapabilityServing {
  quant?: string;
  kv_cache_type?: string;
  flash_attention?: boolean;
  mtp?: boolean;
  moe_cpu_offload_layers?: number;
  ubatch?: number;
  reasoning_mode?: string;
  reasoning_budget?: number;
}
interface LlamaConfigCapabilitySampling {
  temperature?: number;
  top_p?: number;
  top_k?: number;
  min_p?: number;
  presence_penalty?: number;
  repetition_penalty?: number;
}
interface LlamaConfigCapabilityMaxOutput {
  reasoning_budget?: number;
  answer_tokens?: number;
}
interface LlamaConfigCapability {
  context_length?: number;
  input_modalities?: string[];
  output_modalities?: string[];
  tool_calling?: boolean;
  reasoning?: string;
  serving?: LlamaConfigCapabilityServing;
  sampling?: LlamaConfigCapabilitySampling;
  max_output?: LlamaConfigCapabilityMaxOutput;
  sources?: Record<string, ServingSource>;
}
interface LlamaConfigModel {
  name?: string;
  aliases?: string[];
  cmd?: string;
  ttl?: number;
  checkEndpoint?: string;
  metadata?: LlamaConfigCapability;
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

/** Space/newline-separated positional token split, shared by `extractFlags` and
 * `extractRecipe` below -- both scan the SAME fully-expanded command string for a
 * different flag subset, never a general argv parser (this repo's `cmd:` blocks are
 * space/newline-separated flag pairs, never quoted values). */
function tokenizeCmd(cmdText: string): string[] {
  return cmdText.split(/\s+/).filter((t) => t.length > 0);
}

/** `flag`'s immediately-following token, or `undefined` when the flag itself isn't
 * present. A flag not present in the command resolves to `null` one level up in every
 * caller — never a guessed default — this is the raw "found it or not" primitive. */
function valueAfterToken(tokens: string[], flag: string): string | undefined {
  const index = tokens.indexOf(flag);
  return index >= 0 ? tokens[index + 1] : undefined;
}

function toIntFlag(raw: string | undefined): number | null {
  if (raw === undefined || !/^\d+$/.test(raw)) return null;
  return Number.parseInt(raw, 10);
}

/** Pulls the fixed flag set the Ladders/Serving views care about (`-c`, `-ctk`, `-ctv`,
 * `-fa`, `-ngl`) out of a fully-expanded command string — a simple positional token scan,
 * not a general argv parser (this repo's `cmd:` blocks are space/newline-separated flag
 * pairs, never quoted values). A flag not present in the command resolves to `null`,
 * never a guessed default. `allFlags` (WP-1) is the additive full-config sibling of this
 * typed subset -- the subset itself is untouched. */
function extractFlags(cmdText: string): ServingFlags {
  const tokens = tokenizeCmd(cmdText);
  const valueAfter = (flag: string): string | undefined => valueAfterToken(tokens, flag);
  return {
    contextLength: toIntFlag(valueAfter("-c")),
    kvCacheTypeK: valueAfter("-ctk") ?? null,
    kvCacheTypeV: valueAfter("-ctv") ?? null,
    flashAttention: valueAfter("-fa") ?? null,
    gpuLayers: toIntFlag(valueAfter("-ngl")),
    allFlags: extractAllFlags(tokens),
  };
}

/** Served-recipe projection (WP-1, `plans/served-recipe-p50-prefill-and-decode.md`) --
 * artifact + MTP + expert placement + KV types, the config-side half of the join
 * (`apps/api/src/serving/recipe.ts` is the record-side half). Feeds the raw token values
 * into `recipe.ts`'s `projectRecipe` (F1: one shared owner of the `-ctk`/`-ctv` -> `"f16"`
 * default and the `-md`/`--spec-type`/`-ncmoe` -> `null`-on-absence posture) rather than
 * re-deriving those defaults here a second time. "MTP" is `specType !== null || draftPath
 * !== null` (`docs/model-capabilities.md:27`), computed by every reader of this recipe
 * rather than duplicated as a redundant field here. */
function extractRecipe(cmdText: string): ServingRecipe {
  const tokens = tokenizeCmd(cmdText);
  const valueAfter = (flag: string): string | undefined => valueAfterToken(tokens, flag);
  const projected = projectRecipe({
    specType: valueAfter("--spec-type") ?? null,
    draftPath: valueAfter("-md") ?? null,
    moeCpuLayers: toIntFlag(valueAfter("-ncmoe")),
    kvK: valueAfter("-ctk") ?? null,
    kvV: valueAfter("-ctv") ?? null,
  });
  return {
    modelPath: valueAfter("-m") ?? null,
    draftPath: projected.draftPath,
    specType: projected.specType,
    moeCpuLayers: projected.moeCpuLayers,
    kvK: projected.kvK,
    kvV: projected.kvV,
  };
}

/** `metadata:` -> the typed capability block (WP-2, `docs/model-capabilities.md`; extended by
 * WP-1 of `plans/bench-dashboard-serving-control.md` with `sampling`, `max_output` and the
 * `sources` provenance map, none of which this reader carried before). `null`
 * when a model carries no `metadata:` key at all -- a real serving fact for a config
 * predating WP-1, never a parse error. A present block is trusted field-by-field (WP-1's
 * exit bar already verifies every real served model's block is complete); any individual
 * field this parser can't find resolves to `null`, never a guessed default, matching
 * `extractFlags`'s own posture above. `??` (not `||`) throughout so a legitimate `false`
 * (e.g. `tool_calling: false`) is never mistaken for "absent". */
function extractCapability(metadata: LlamaConfigCapability | undefined): ServingCapability | null {
  if (metadata === undefined) return null;
  const serving = metadata.serving ?? {};
  return {
    contextLength: metadata.context_length ?? null,
    inputModalities: metadata.input_modalities ?? [],
    outputModalities: metadata.output_modalities ?? [],
    toolCalling: metadata.tool_calling ?? null,
    reasoning: metadata.reasoning ?? null,
    serving: {
      quant: serving.quant ?? null,
      kvCacheType: serving.kv_cache_type ?? null,
      flashAttention: serving.flash_attention ?? null,
      mtp: serving.mtp ?? null,
      moeCpuOffloadLayers: serving.moe_cpu_offload_layers ?? null,
      ubatch: serving.ubatch ?? null,
      reasoningMode: serving.reasoning_mode ?? null,
      reasoningBudget: serving.reasoning_budget ?? null,
    },
    sampling:
      metadata.sampling === undefined
        ? null
        : {
            temperature: metadata.sampling.temperature ?? null,
            topP: metadata.sampling.top_p ?? null,
            topK: metadata.sampling.top_k ?? null,
            minP: metadata.sampling.min_p ?? null,
            presencePenalty: metadata.sampling.presence_penalty ?? null,
            repetitionPenalty: metadata.sampling.repetition_penalty ?? null,
          },
    maxOutput:
      metadata.max_output === undefined
        ? null
        : {
            reasoningBudget: metadata.max_output.reasoning_budget ?? null,
            answerTokens: metadata.max_output.answer_tokens ?? null,
          },
    // Carried through VERBATIM, entry for entry, with no normalization and nothing dropped:
    // validating provenance is `dashboard/scripts/check-serving-recipes.ts`'s job, and a
    // malformed entry that this reader silently repaired or discarded would be invisible to
    // that gate. The reader's contract is "what the YAML says", same as every field above.
    sources: metadata.sources ?? null,
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
    const expandedCmd = expandMacros(model.cmd ?? "", macros);
    aliases.push({
      modelKey,
      name: model.name ?? null,
      aliases: modelAliases,
      ttl: typeof model.ttl === "number" ? model.ttl : null,
      checkEndpoint: model.checkEndpoint ?? null,
      flags: extractFlags(expandedCmd),
      capability: extractCapability(model.metadata),
      recipe: extractRecipe(expandedCmd),
    });
  }
  return aliases;
}

export async function readServingConfig(path: string): Promise<ServingResponse> {
  const text = await Bun.file(path).text();
  return { configPath: path, models: parseServedAliases(text) };
}
