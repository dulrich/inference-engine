// `bun run check:recipes` -- the capability-recipe conformance gate for `llama/config.yaml`'s
// served CHAT entries (WP-1, `plans/bench-dashboard-serving-control.md`). It makes the required
// key set in `docs/model-capabilities.md` mechanically checkable instead of review-enforced.
//
// Why it lives in `dashboard/scripts/`: `Bun.YAML.parse` (used by
// `apps/api/src/serving/llamaConfig.ts`) is the only YAML parser available anywhere on this
// repo's implementation boundary without adding a dependency -- `llama/bakeoff` is stdlib-only
// by contract and `router/*.mjs` is plain Node over JSON. It reuses `parseServedAliases` rather
// than parsing the file a second time, so the gate and the dashboard can never disagree about
// what the config says.
//
// Pure function of ONE YAML file: no network, no live service, no write path of any kind. The
// default config path is resolved from `import.meta.dir`, never from the cwd, so `bun run
// check:recipes` checks the repo's own file wherever it is invoked from.
//
// Three classes of value, per the ground rules in `docs/model-capabilities.md`:
//   - ADVISORY (`sampling.*`, `max_output.answer_tokens`) -- publisher-stated, cites a source.
//   - PROJECTION (`context_length`, `max_output.reasoning_budget`, `serving.*`, the modality
//     lists, `tool_calling`) -- derived from the served `cmd:`, so checked for EQUALITY with
//     the flag, or with that flag's documented default when the flag is absent. Citing a
//     projection would be a category error; the gate never asks for one.
//   - LIVE-PROBED (`reasoning`) -- a response-body fact no flag determines, so the gate checks
//     only that the value is a member of the enumeration and leaves the evidence bar in the doc.
import { join } from "node:path";
import { parseServedAliases } from "../apps/api/src/serving/llamaConfig.ts";
import { SERVING_VERIFIED_DATE_PATTERN, type ServingAlias } from "../packages/shared/src/serving.ts";

/** `<repo>/llama/config.yaml`, resolved from this file's own location. */
export const DEFAULT_CONFIG_PATH = join(import.meta.dir, "../../llama/config.yaml");

/** The five approved source kinds (`docs/model-capabilities.md`), as the host each is served
 * from -- the only mechanical test available for "is this an approved kind". `huggingface.co`
 * covers two of the five (the lab's own model page and the unsloth page for the served GGUF);
 * which of the two a given url is, is a review question, not a gate question. */
const APPROVED_SOURCE_HOSTS = new Map<string, string>([
  ["huggingface.co", "lab or unsloth HF model page"],
  ["docs.vllm.ai", "vLLM recipe"],
  ["docs.sglang.ai", "SGLang recipe"],
  ["docs.unsloth.ai", "unsloth serving guide"],
]);

/** The six sampling keys the Open WebUI sync allowlists, in dotted-path spelling. */
const SAMPLING_KEYS = [
  "temperature",
  "top_p",
  "top_k",
  "min_p",
  "presence_penalty",
  "repetition_penalty",
] as const;

/** Exactly the advisory keys -- seven entries in `sources`, no more, no fewer. */
const ADVISORY_KEYS = [
  ...SAMPLING_KEYS.map((k) => `sampling.${k}`),
  "max_output.answer_tokens",
];

const REASONING_LEVELS = new Set(["none", "budgeted", "request-controllable", "always-on"]);
const INPUT_MODALITIES = new Set(["text", "image"]);
const OUTPUT_MODALITIES = new Set(["text", "embedding", "score"]);

/** One non-conformance. `rule` is the stable machine-ish name; `detail` is what a human needs
 * to fix it without opening the gate's source. */
export interface RecipeFinding {
  modelKey: string;
  rule: string;
  detail: string;
}

export function formatFinding(finding: RecipeFinding): string {
  return `${finding.modelKey}: ${finding.rule} — ${finding.detail}`;
}

/** `undefined` when the flag is absent from the expanded `cmd:`; `null` for a boolean-style
 * flag with no value token; otherwise the value token. Reads the ordered `allFlags` list
 * `parseServedAliases` already extracted -- never a second tokenizer. */
function flagValue(alias: ServingAlias, flag: string): string | null | undefined {
  const entry = alias.flags.allFlags.find((f) => f.flag === flag);
  return entry === undefined ? undefined : entry.value;
}

function hasFlag(alias: ServingAlias, flag: string): boolean {
  return flagValue(alias, flag) !== undefined;
}

function intFlag(alias: ServingAlias, flag: string): number | null {
  const raw = flagValue(alias, flag);
  if (typeof raw !== "string" || !/^-?\d+$/.test(raw)) return null;
  return Number.parseInt(raw, 10);
}

/** Embed (`--embeddings`) and rerank (`--reranking`) entries carry no `sampling`, no
 * `max_output` and no provenance by the contract doc's own rule ("the whole block is absent on
 * non-chat models"), so the gate skips them entirely. A model whose `output_modalities` says it
 * returns something other than text is skipped on the same ground. A model with NO `metadata:`
 * at all is NOT skipped -- it is exactly the case the gate exists to catch. */
function isChatEntry(alias: ServingAlias): boolean {
  if (hasFlag(alias, "--embeddings") || hasFlag(alias, "--reranking")) return false;
  const outputs = alias.capability?.outputModalities;
  if (outputs !== undefined && outputs.length > 0 && !outputs.includes("text")) return false;
  return true;
}

function checkSources(alias: ServingAlias, findings: RecipeFinding[]): void {
  const modelKey = alias.modelKey;
  const sources = alias.capability?.sources ?? null;
  if (sources === null) {
    findings.push({ modelKey, rule: "sources-missing", detail: "no `metadata.sources` map; the seven advisory keys each need a citation or an explicit gap record" });
    return;
  }
  const present = Object.keys(sources);
  const missing = ADVISORY_KEYS.filter((k) => !present.includes(k));
  const extra = present.filter((k) => !ADVISORY_KEYS.includes(k));
  if (missing.length > 0 || extra.length > 0) {
    const parts: string[] = [];
    if (missing.length > 0) parts.push(`missing ${missing.join(", ")}`);
    if (extra.length > 0) parts.push(`unexpected ${extra.join(", ")}`);
    findings.push({ modelKey, rule: "sources-keys", detail: `\`sources\` must carry exactly the seven advisory keys — ${parts.join("; ")}` });
  }
  for (const key of present) {
    // One cast, at the boundary: the reader carries YAML values through verbatim (that is what
    // lets this gate see a malformed entry at all), so every field is checked at runtime here.
    const entry = sources[key] as { url?: unknown; verified?: unknown; reason?: unknown } | undefined;
    if (entry === undefined || entry === null || typeof entry !== "object") {
      findings.push({ modelKey, rule: "source-shape", detail: `\`sources."${key}"\` is not a mapping` });
      continue;
    }
    const { url, verified, reason } = entry;
    if (typeof url !== "string" || url.length === 0) {
      findings.push({ modelKey, rule: "source-url", detail: `\`sources."${key}".url\` is missing` });
      continue;
    }
    if (url === "none") {
      if (typeof reason !== "string" || reason.trim().length === 0) {
        findings.push({ modelKey, rule: "source-reason", detail: `\`sources."${key}"\` records url "none" without a \`reason\`` });
      }
      continue;
    }
    let host: string | null = null;
    let path = "";
    try {
      const parsed = new URL(url);
      host = parsed.protocol === "https:" ? parsed.hostname.replace(/^www\./, "") : null;
      path = parsed.pathname;
    } catch {
      host = null;
    }
    if (host === null || !APPROVED_SOURCE_HOSTS.has(host) || path.length <= 1) {
      findings.push({ modelKey, rule: "source-url", detail: `\`sources."${key}".url\` is not one of the approved source kinds (${[...APPROVED_SOURCE_HOSTS.values()].join(", ")}) or the literal "none": ${url}` });
      continue;
    }
    if (typeof verified !== "string" || !SERVING_VERIFIED_DATE_PATTERN.test(verified)) {
      findings.push({ modelKey, rule: "source-verified", detail: `\`sources."${key}".verified\` must be a YYYY-MM-DD date, got ${JSON.stringify(verified)}` });
    }
  }
}

/** The six sampling keys must each be DECLARED: a numeric value (whose citation the `sources`
 * checks above cover), or an explicit gap record. Silence is the one thing that is not allowed
 * -- the do-not-invent rule means an absent key must say "the card is silent", not nothing. */
function checkSamplingDeclared(alias: ServingAlias, findings: RecipeFinding[]): void {
  const sampling = alias.capability?.sampling ?? null;
  const sources = alias.capability?.sources ?? null;
  const valueFor: Record<string, number | null> = {
    temperature: sampling?.temperature ?? null,
    top_p: sampling?.topP ?? null,
    top_k: sampling?.topK ?? null,
    min_p: sampling?.minP ?? null,
    presence_penalty: sampling?.presencePenalty ?? null,
    repetition_penalty: sampling?.repetitionPenalty ?? null,
  };
  for (const key of SAMPLING_KEYS) {
    if (typeof valueFor[key] === "number") continue;
    const gap = sources?.[`sampling.${key}`] as { url?: unknown } | undefined;
    if (gap !== undefined && gap !== null && typeof gap === "object" && gap.url === "none") continue;
    findings.push({ modelKey: alias.modelKey, rule: "sampling-undeclared", detail: `\`sampling.${key}\` has no value and no \`sources."sampling.${key}"\` gap record` });
  }
}

/** Projections: equality with the flag, or with the flag's documented default when it is
 * absent. Every check here is skipped when the underlying metadata key is missing -- that case
 * is already one `missing-key` finding, and reporting it twice would make "one line per
 * non-conforming model" untrue for the common case. */
function checkProjections(alias: ServingAlias, findings: RecipeFinding[]): void {
  const modelKey = alias.modelKey;
  const cap = alias.capability;
  if (cap === null) return;
  const push = (rule: string, detail: string): void => {
    findings.push({ modelKey, rule, detail });
  };

  if (cap.contextLength !== null) {
    const c = intFlag(alias, "-c");
    if (c === null) push("projection", "`context_length` is declared but the `cmd:` carries no `-c` to project it from");
    else if (c !== cap.contextLength) push("projection", `\`context_length\` ${cap.contextLength} ≠ \`-c ${c}\``);
  }

  // `--reasoning-budget` absent means the llama-server default, `-1` (unrestricted) -- so the
  // mirrored `max_output.reasoning_budget` must SAY `-1`, never claim a cap that is not served.
  const budgetFlag = intFlag(alias, "--reasoning-budget");
  const expectedBudget = hasFlag(alias, "--reasoning-budget") ? budgetFlag : -1;
  const declaredMax = cap.maxOutput?.reasoningBudget ?? null;
  if (declaredMax !== null && declaredMax !== expectedBudget) {
    push("projection", `\`max_output.reasoning_budget\` ${declaredMax} ≠ ${expectedBudget} (${hasFlag(alias, "--reasoning-budget") ? "`--reasoning-budget` in `cmd:`" : "the flag is absent, so the documented default"})`);
  }
  if (cap.serving.reasoningBudget !== null && cap.serving.reasoningBudget !== expectedBudget) {
    push("projection", `\`serving.reasoning_budget\` ${cap.serving.reasoningBudget} ≠ ${expectedBudget} (${hasFlag(alias, "--reasoning-budget") ? "`--reasoning-budget` in `cmd:`" : "the flag is absent"})`);
  }
  if (cap.serving.reasoningBudget === null && hasFlag(alias, "--reasoning-budget")) {
    push("projection", `\`cmd:\` carries \`--reasoning-budget ${budgetFlag}\` but \`serving.reasoning_budget\` is absent`);
  }

  // The three remaining optional `serving.*` keys: present iff the flag is present, equal when
  // both are ("absent means macro default, never unknown" cuts both ways).
  const optional: Array<[string, string, number | string | null]> = [
    ["serving.ubatch", "-ub", cap.serving.ubatch],
    ["serving.moe_cpu_offload_layers", "-ncmoe", cap.serving.moeCpuOffloadLayers],
    ["serving.reasoning_mode", "--reasoning", cap.serving.reasoningMode],
  ];
  for (const [key, flag, declared] of optional) {
    const raw = flagValue(alias, flag);
    if (declared === null && raw === undefined) continue;
    if (declared === null) {
      push("projection", `\`cmd:\` carries \`${flag} ${String(raw)}\` but \`${key}\` is absent`);
      continue;
    }
    if (raw === undefined) {
      push("projection", `\`${key}\` is declared (${String(declared)}) but the \`cmd:\` carries no \`${flag}\``);
      continue;
    }
    const expected = typeof declared === "number" ? intFlag(alias, flag) : raw;
    if (String(expected) !== String(declared)) {
      push("projection", `\`${key}\` ${String(declared)} ≠ \`${flag} ${String(raw)}\``);
    }
  }

  if (cap.serving.kvCacheType !== null) {
    const ctk = flagValue(alias, "-ctk");
    const expectedKv = typeof ctk === "string" ? ctk : hasFlag(alias, "-ctv") ? String(flagValue(alias, "-ctv")) : "f16";
    if (cap.serving.kvCacheType !== expectedKv) {
      push("projection", `\`serving.kv_cache_type\` "${cap.serving.kvCacheType}" ≠ "${expectedKv}" (${typeof ctk === "string" ? "`-ctk` in `cmd:`" : "no `-ctk`/`-ctv`, so the llama.cpp default"})`);
    }
  }

  if (cap.serving.flashAttention !== null) {
    const fa = flagValue(alias, "-fa") === "on";
    if (cap.serving.flashAttention !== fa) {
      push("projection", `\`serving.flash_attention\` ${cap.serving.flashAttention} ≠ ${fa} (\`-fa on\` ${fa ? "is" : "is not"} in \`cmd:\`)`);
    }
  }

  if (cap.serving.quant !== null) {
    const modelPath = alias.recipe?.modelPath ?? null;
    const file = modelPath === null ? null : (modelPath.split("/").pop() ?? null);
    if (file === null) push("projection", `\`serving.quant\` "${cap.serving.quant}" cannot be checked — the \`cmd:\` carries no \`-m\``);
    else if (!file.includes(cap.serving.quant)) {
      push("projection", `\`serving.quant\` "${cap.serving.quant}" does not appear in the served GGUF filename ${file}`);
    }
  }

  if (cap.inputModalities.length > 0) {
    const bad = cap.inputModalities.filter((m) => !INPUT_MODALITIES.has(m));
    if (bad.length > 0) push("vocabulary", `\`input_modalities\` carries ${bad.join(", ")} — vocabulary is text, image`);
    const claimsImage = cap.inputModalities.includes("image");
    const mmproj = hasFlag(alias, "--mmproj");
    if (claimsImage !== mmproj) {
      push("projection", `\`input_modalities\` ${claimsImage ? "claims" : "omits"} "image" but the \`cmd:\` ${mmproj ? "carries" : "carries no"} \`--mmproj\``);
    }
  }
  if (cap.outputModalities.length > 0) {
    const bad = cap.outputModalities.filter((m) => !OUTPUT_MODALITIES.has(m));
    if (bad.length > 0) push("vocabulary", `\`output_modalities\` carries ${bad.join(", ")} — vocabulary is text, embedding, score`);
  }
  if (cap.reasoning !== null && !REASONING_LEVELS.has(cap.reasoning)) {
    push("vocabulary", `\`reasoning\` "${cap.reasoning}" is not one of ${[...REASONING_LEVELS].join(", ")}`);
  }
}

/** Every NON-OPTIONAL key in the contract doc's Fields table, plus the two `max_output` keys.
 * The four optional `serving.*` keys are deliberately absent from this list — they are checked
 * against their flags in `checkProjections` instead. */
function checkRequiredKeys(alias: ServingAlias, findings: RecipeFinding[]): void {
  const modelKey = alias.modelKey;
  const cap = alias.capability;
  if (cap === null) {
    findings.push({ modelKey, rule: "missing-key", detail: "no `metadata:` block at all" });
    return;
  }
  const required: Array<[string, boolean]> = [
    ["context_length", cap.contextLength !== null],
    ["input_modalities", cap.inputModalities.length > 0],
    ["output_modalities", cap.outputModalities.length > 0],
    ["tool_calling", cap.toolCalling !== null],
    ["reasoning", cap.reasoning !== null],
    ["serving.quant", cap.serving.quant !== null],
    ["serving.kv_cache_type", cap.serving.kvCacheType !== null],
    ["serving.flash_attention", cap.serving.flashAttention !== null],
    ["serving.mtp", cap.serving.mtp !== null],
    ["max_output.reasoning_budget", (cap.maxOutput?.reasoningBudget ?? null) !== null],
    ["max_output.answer_tokens", (cap.maxOutput?.answerTokens ?? null) !== null],
  ];
  const missing = required.filter(([, ok]) => !ok).map(([key]) => key);
  for (const key of missing) {
    findings.push({ modelKey, rule: "missing-key", detail: `\`${key}\` is required on every served chat entry` });
  }
}

/** Every non-conformance in `configText`, in config order. An empty array means the file
 * conforms. Chat entries only. */
export function checkServingRecipes(configText: string): RecipeFinding[] {
  const findings: RecipeFinding[] = [];
  for (const alias of parseServedAliases(configText)) {
    if (!isChatEntry(alias)) continue;
    checkRequiredKeys(alias, findings);
    checkSamplingDeclared(alias, findings);
    checkSources(alias, findings);
    checkProjections(alias, findings);
  }
  return findings;
}

export async function checkServingRecipesFile(path: string): Promise<RecipeFinding[]> {
  return checkServingRecipes(await Bun.file(path).text());
}

/** `--config <path>` (or `--config=<path>`), defaulting to the repo's own `llama/config.yaml`. */
export function parseConfigArg(argv: string[]): string {
  const index = argv.indexOf("--config");
  if (index >= 0 && argv[index + 1] !== undefined) return argv[index + 1] as string;
  const inline = argv.find((a) => a.startsWith("--config="));
  if (inline !== undefined) return inline.slice("--config=".length);
  return DEFAULT_CONFIG_PATH;
}

if (import.meta.main) {
  const path = parseConfigArg(Bun.argv.slice(2));
  const findings = await checkServingRecipesFile(path);
  for (const finding of findings) console.log(formatFinding(finding));
  if (findings.length > 0) {
    console.log(`${findings.length} non-conforming entr${findings.length === 1 ? "y" : "ies"} in ${path}`);
    process.exit(1);
  }
  console.log(`ok — every served chat entry in ${path} conforms to docs/model-capabilities.md`);
}
