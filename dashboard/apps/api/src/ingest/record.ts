import { readdir } from "node:fs/promises";
import { basename, join } from "node:path";

// Reads one `runs/bakeoff/<experiment-id>/` directory and classifies it as `v2` (parsed
// and normalized, ready for `db/experiments.ts replaceExperiment`), `v1` (frozen input
// contract: "Pre-existing v1 records on disk remain as-is — the DB plan ingests v2 only",
// `llama/bakeoff/README.md` "Schema v2 (FROZEN 2026-08-23)"), or `invalid` (a directory
// that claims v2 but is unreadable/malformed — logged and skipped, never crashes the rest
// of an ingest run).
//
// Field-to-file split (frozen, same doc): `config.json` (run-level metadata + lineage +
// `sweep_cell`/`wilson_summary`), `prompts.jsonl` (quality-corpus prompt text),
// `completions.jsonl` (every raw completion verbatim), `samples.jsonl` (one JSON object
// per line: numeric timing/sample stats, correctness verdicts, classifier output — every
// `type` this producer emits AND any this ingest hasn't seen yet, per the brief's pin
// "type is data, not an enum to validate against").

export interface NormalizedTrial {
  lineIndex: number;
  type: string;
  trial: number | null;
  samplingArm: string | null;
  temperature: number | null;
  topP: number | null;
  topK: number | null;
  samplerSeed: number | null;
  haystackSeed: number | null;
  corpusManifestId: string | null;
  haystackSha256: string | null;
  classifierVerdict: string | null;
  classifierShape: string | null;
  promptTokens: number | null;
  completionTokens: number | null;
  finishReason: string | null;
  positionFraction: number | null;
  task: string | null;
  cellId: string | null;
  correct: boolean | null;
  raw: unknown;
}

export interface NormalizedMetric {
  lineIndex: number | null;
  metricType: string;
  roundType: string | null;
  roundIndex: number | null;
  bytes: number | null;
  firstGenerationTokenMs: number | null;
  observedDecodeTokS: number | null;
  promptTokens: number | null;
  cachedTokens: number | null;
  cacheHitRatio: number | null;
  cacheAssertionPassed: boolean | null;
  raw: unknown;
}

export interface NormalizedRecord {
  id: string;
  schemaVersion: 2;
  description: string | null;
  timestamp: string | null;
  hfRepo: string | null;
  hfRevision: string | null;
  filename: string | null;
  fileSize: number | null;
  sha256: string | null;
  llamaCppBuild: string | null;
  sweepCellId: string | null;
  sweepAlias: string | null;
  sweepServerFlags: unknown | null;
  sweepCommand: string | null;
  wilsonSummary: unknown | null;
  rawConfig: unknown;
  rawPrompts: unknown[];
  rawCompletions: unknown[];
  trials: NormalizedTrial[];
  metrics: NormalizedMetric[];
}

export type ReadRecordResult =
  | { kind: "v2"; record: NormalizedRecord }
  | { kind: "v1" }
  | { kind: "invalid"; reason: string };

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function asBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readJson(path: string): Promise<unknown> {
  const text = await Bun.file(path).text();
  return JSON.parse(text);
}

/** Parses a `.jsonl` file into an array, one entry per non-blank line. Throws on the
 * first malformed line — an ingest-classified `invalid` record, never a silently
 * truncated one. */
async function readJsonl(path: string): Promise<unknown[]> {
  const text = await Bun.file(path).text();
  const lines = text.split("\n").filter((line) => line.trim().length > 0);
  return lines.map((line, index) => {
    try {
      return JSON.parse(line) as unknown;
    } catch (error) {
      throw new Error(`${path}:${index + 1}: ${error instanceof Error ? error.message : String(error)}`);
    }
  });
}

function normalizeSweepCell(config: Record<string, unknown>): {
  sweepCellId: string | null;
  sweepAlias: string | null;
  sweepServerFlags: unknown | null;
  sweepCommand: string | null;
} {
  const cell = config.sweep_cell;
  if (!isRecord(cell)) return { sweepCellId: null, sweepAlias: null, sweepServerFlags: null, sweepCommand: null };
  return {
    sweepCellId: asString(cell.cell_id),
    sweepAlias: asString(cell.alias),
    sweepServerFlags: cell.server_flags ?? null,
    sweepCommand: asString(cell.command),
  };
}

function normalizeTrial(row: unknown, lineIndex: number): NormalizedTrial {
  const r = isRecord(row) ? row : {};
  return {
    lineIndex,
    type: asString(r.type) ?? "unknown",
    trial: asNumber(r.trial) ?? asNumber(r.trial_index),
    samplingArm: asString(r.sampling_arm),
    temperature: asNumber(r.temperature),
    topP: asNumber(r.top_p),
    topK: asNumber(r.top_k),
    samplerSeed: asNumber(r.sampler_seed),
    haystackSeed: asNumber(r.haystack_seed),
    corpusManifestId: asString(r.corpus_manifest_id),
    haystackSha256: asString(r.haystack_sha256),
    classifierVerdict: asString(r.classifier_verdict),
    classifierShape: asString(r.classifier_shape),
    promptTokens: asNumber(r.prompt_tokens),
    completionTokens: asNumber(r.completion_tokens),
    finishReason: asString(r.finish_reason),
    positionFraction: asNumber(r.position_fraction),
    task: asString(r.task),
    cellId: asString(r.cell_id),
    correct: asBoolean(r.correct),
    raw: row,
  };
}

function normalizeTimingMetric(row: unknown, lineIndex: number): NormalizedMetric {
  const r = isRecord(row) ? row : {};
  return {
    lineIndex,
    metricType: "timing_round",
    roundType: asString(r.run_state),
    roundIndex: asNumber(r.round_index),
    bytes: null,
    firstGenerationTokenMs: asNumber(r.first_generation_token_ms),
    observedDecodeTokS: asNumber(r.observed_decode_tok_s),
    promptTokens: asNumber(r.prompt_tokens),
    cachedTokens: asNumber(r.cached_tokens),
    cacheHitRatio: asNumber(r.cache_hit_ratio),
    cacheAssertionPassed: asBoolean(r.cache_assertion_passed),
    raw: row,
  };
}

function vramMetrics(config: Record<string, unknown>): NormalizedMetric[] {
  const vram = config.vram;
  if (!isRecord(vram)) return [];
  const entries: [string, string][] = [
    ["baseline_bytes", "vram_baseline"],
    ["loaded_bytes", "vram_loaded"],
    ["after_teardown_bytes", "vram_after_teardown"],
  ];
  const metrics: NormalizedMetric[] = [];
  for (const [key, metricType] of entries) {
    const bytes = asNumber(vram[key]);
    if (bytes === null) continue;
    metrics.push({
      lineIndex: null,
      metricType,
      roundType: null,
      roundIndex: null,
      bytes,
      firstGenerationTokenMs: null,
      observedDecodeTokS: null,
      promptTokens: null,
      cachedTokens: null,
      cacheHitRatio: null,
      cacheAssertionPassed: null,
      raw: vram,
    });
  }
  return metrics;
}

export async function readRecordDir(dir: string): Promise<ReadRecordResult> {
  const id = basename(dir);
  const configPath = join(dir, "config.json");

  let config: unknown;
  try {
    config = await readJson(configPath);
  } catch (error) {
    return { kind: "invalid", reason: `config.json: ${error instanceof Error ? error.message : String(error)}` };
  }
  if (!isRecord(config)) return { kind: "invalid", reason: "config.json is not a JSON object" };

  if (config.schema_version !== 2) return { kind: "v1" };

  let prompts: unknown[];
  let completions: unknown[];
  let samplesRaw: unknown[];
  try {
    [prompts, completions, samplesRaw] = await Promise.all([
      readJsonl(join(dir, "prompts.jsonl")),
      readJsonl(join(dir, "completions.jsonl")),
      readJsonl(join(dir, "samples.jsonl")),
    ]);
  } catch (error) {
    return { kind: "invalid", reason: error instanceof Error ? error.message : String(error) };
  }

  const lineage = isRecord(config.lineage) ? config.lineage : {};
  const sweep = normalizeSweepCell(config);

  const trials: NormalizedTrial[] = [];
  const metrics: NormalizedMetric[] = vramMetrics(config);
  samplesRaw.forEach((row, lineIndex) => {
    const type = isRecord(row) ? asString(row.type) : null;
    if (type === "timing_round") metrics.push(normalizeTimingMetric(row, lineIndex));
    else trials.push(normalizeTrial(row, lineIndex));
  });

  const record: NormalizedRecord = {
    id,
    schemaVersion: 2,
    description: asString(config.description),
    timestamp: asString(config.timestamp),
    hfRepo: asString(lineage.hf_repo),
    hfRevision: asString(lineage.hf_revision),
    filename: asString(lineage.filename),
    fileSize: asNumber(lineage.file_size),
    sha256: asString(lineage.sha256),
    llamaCppBuild: asString(lineage.llama_cpp_build),
    sweepCellId: sweep.sweepCellId,
    sweepAlias: sweep.sweepAlias,
    sweepServerFlags: sweep.sweepServerFlags,
    sweepCommand: sweep.sweepCommand,
    // `wilson_summary` is always present as a KEY on a v2 record (frozen contract) but its
    // VALUE is legitimately `null` — `?? null` here is a default for an absent key on a
    // malformed record, not a stand-in for the frozen null.
    wilsonSummary: config.wilson_summary ?? null,
    rawConfig: config,
    rawPrompts: prompts,
    rawCompletions: completions,
    trials,
    metrics,
  };
  return { kind: "v2", record };
}

/** Every immediate subdirectory of `recordsDir` — the ingest CLI's scan list. Not
 * recursive: `runs/bakeoff/<experiment-id>/` is a flat, one-level layout (frozen
 * contract). Non-directory entries (stray files) are skipped, not counted as records. */
export async function listRecordDirs(recordsDir: string): Promise<string[]> {
  const entries = await readdir(recordsDir, { withFileTypes: true });
  return entries
    .filter((entry) => entry.isDirectory())
    .map((entry) => join(recordsDir, entry.name))
    .sort();
}
