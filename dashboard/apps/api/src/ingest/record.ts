import { existsSync } from "node:fs";
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
  samplingArm: string | null;
  battery: string[] | null;
  vramProfile: string | null;
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
/** `sweep_cell.battery` — a task-name array (e.g. `["needle","vram"]`). Not a validated
 * enum here (frozen-contract discipline elsewhere in this module: shape-check, never
 * value-validate against a task list this ingest doesn't own). */
function asStringArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.every((v) => typeof v === "string") ? (value as string[]) : null;
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
  samplingArm: string | null;
  battery: string[] | null;
} {
  const cell = config.sweep_cell;
  if (!isRecord(cell)) {
    return {
      sweepCellId: null,
      sweepAlias: null,
      sweepServerFlags: null,
      sweepCommand: null,
      samplingArm: null,
      battery: null,
    };
  }
  return {
    sweepCellId: asString(cell.cell_id),
    sweepAlias: asString(cell.alias),
    sweepServerFlags: cell.server_flags ?? null,
    sweepCommand: asString(cell.command),
    samplingArm: asString(cell.sampling_arm),
    battery: asStringArray(cell.battery),
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
    samplingArm: sweep.samplingArm,
    battery: sweep.battery,
    // `vram_profile` — top-level on `config.json` (WP-B addition), absent on any record
    // predating it. Never guessed from `sweep_cell` or elsewhere — absent stays NULL.
    vramProfile: asString(config.vram_profile),
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

export type ReadLegacyTimingResult =
  | { kind: "v1-timing"; record: NormalizedRecord }
  | { kind: "v1" }
  | { kind: "invalid"; reason: string };

/** Legacy dirs commonly carry alias/identity as a top-level `config.alias` key (pre-v2
 * shape — `sweep_cell` did not exist yet), never a `sweep_cell.alias`; the latter is
 * checked too, defensively, for any straggler dir already close to the v2 transition.
 * Absent/odd either way returns `null` — the caller's `id` (dir basename) is the identity
 * that always survives (frozen brief pin: "ingest under its dir-derived identity"). */
function legacyAlias(config: Record<string, unknown>): string | null {
  const topLevel = asString(config.alias);
  if (topLevel !== null) return topLevel;
  const cell = config.sweep_cell;
  return isRecord(cell) ? asString(cell.alias) : null;
}

/** The WP-4 allowlisted timing-only ingest side door
 * (`plans/bakeoff-timing-records-and-estimates.md`) for pre-v2 (`schema_version !== 2`)
 * record dirs. `readRecordDir` above classifies every such dir `v1` and returns before
 * ever reading `samples.jsonl` (`:245` — the v2 gate stays byte-identical for every OTHER
 * purpose); this function is the only place a v1 dir's `samples.jsonl` is read, and it
 * reuses this module's existing machinery (`normalizeTimingMetric`, the `asString`/
 * `isRecord`/`readJson`/`readJsonl` primitives) rather than a second, parallel
 * normalization path.
 *
 * Selection is content-based — does `samples.jsonl` contain at least one `timing_round`
 * row? — never a `*-full` directory-name glob (two of the twelve qualifying legacy dirs on
 * disk, `qwen38-27b-iq3s-kv-e-alt-q4q4-c120000` and `wp1-dry-run-nemotron-4b`, don't match
 * that suffix). A dir with `timing_round` rows contributes ONLY those rows as `metrics` —
 * `trials` is always `[]` (no needle/sample/curve/quality content from a legacy dir, ever),
 * `wilsonSummary`/`samplingArm`/`battery`/`vramProfile`/`sweepServerFlags`/`sweepCommand`/
 * `sweepCellId` are always `null` (those are v2-only or quality-adjacent concepts this side
 * door does not resurrect), and `rawPrompts`/`rawCompletions` are always `[]` (this
 * function never reads `prompts.jsonl`/`completions.jsonl` — out of scope for a
 * timing-only door). `schemaVersion: 2` on the returned record is a `replaceExperiment`
 * write-path artifact (that function hardcodes `schema_version = 2` for every row it
 * writes, `db/experiments.ts`) — never a claim the source directory declared v2 on disk;
 * true provenance is the caller's separate `schema_origin` write (`ingest/run.ts`,
 * `db/schema.ts`). */
export async function readLegacyTimingRecord(dir: string): Promise<ReadLegacyTimingResult> {
  const id = basename(dir);
  const configPath = join(dir, "config.json");

  let config: unknown;
  try {
    config = await readJson(configPath);
  } catch (error) {
    return { kind: "invalid", reason: `config.json: ${error instanceof Error ? error.message : String(error)}` };
  }
  if (!isRecord(config)) return { kind: "invalid", reason: "config.json is not a JSON object" };
  // A real v2 record is `readRecordDir`'s business, never this side door's.
  if (config.schema_version === 2) return { kind: "v1" };

  const samplesPath = join(dir, "samples.jsonl");
  if (!existsSync(samplesPath)) return { kind: "v1" };

  let samplesRaw: unknown[];
  try {
    samplesRaw = await readJsonl(samplesPath);
  } catch (error) {
    return { kind: "invalid", reason: error instanceof Error ? error.message : String(error) };
  }

  const metrics: NormalizedMetric[] = [];
  samplesRaw.forEach((row, lineIndex) => {
    const type = isRecord(row) ? asString(row.type) : null;
    if (type === "timing_round") metrics.push(normalizeTimingMetric(row, lineIndex));
  });
  // No timing_round rows -- fully skipped, same outcome as if this side door didn't exist.
  if (metrics.length === 0) return { kind: "v1" };

  const lineage = isRecord(config.lineage) ? config.lineage : {};

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
    sweepCellId: null,
    sweepAlias: legacyAlias(config),
    sweepServerFlags: null,
    sweepCommand: null,
    samplingArm: null,
    battery: null,
    vramProfile: null,
    wilsonSummary: null,
    rawConfig: config,
    rawPrompts: [],
    rawCompletions: [],
    trials: [],
    metrics,
  };
  return { kind: "v1-timing", record };
}

function hasConfigJson(dir: string): boolean {
  return existsSync(join(dir, "config.json"));
}

/** Every immediate subdirectory of `dir`, sorted. Non-directory entries (stray files) are
 * skipped. Returns `[]` (never throws) for a `dir` that itself does not exist / is not
 * readable — callers treat an unreadable batch-run candidate the same as one with no
 * subdirectories at all (falls through to `invalid`, never crashes the scan). */
async function immediateSubDirs(dir: string): Promise<string[]> {
  try {
    const entries = await readdir(dir, { withFileTypes: true });
    return entries
      .filter((entry) => entry.isDirectory())
      .map((entry) => join(dir, entry.name))
      .sort();
  } catch {
    return [];
  }
}

/** Two-level-aware scan of `recordsDir` (WP-A: `plans/bench-dashboard-run-context-
 * surfacing.md` WP-A). Every first-level entry is classified independently:
 *
 * - Contains `config.json` directly → a record dir (the original flat contract,
 *   preserved verbatim — `readRecordDir` classifies it `v2`/`v1`/`invalid` as before).
 * - Does NOT contain `config.json`, but at least one of ITS immediate subdirs does → a
 *   batch run dir (e.g. `runs/bakeoff/wp5-validation/<cell-id>/config.json`, live
 *   example — not depended on by tests, which build their own fixtures). Every immediate
 *   subdir of a batch run dir is scanned as a record dir in its own right — exactly one
 *   extra level, matching the flat contract's own invalid/v1/v2 classification for each
 *   (a subdir among them that itself lacks `config.json` still becomes `invalid`, same
 *   as it would in a flat layout).
 * - Neither → the first-level dir itself is returned as-is; `readRecordDir` then
 *   classifies it `invalid` (missing `config.json`) exactly as the pre-two-level
 *   contract did. This is also the terminal case for anything nested deeper than one
 *   extra level: a third level of `config.json` is never discovered (`immediateSubDirs`
 *   only ever looks one level below the first-level entry).
 */
export async function listRecordDirs(recordsDir: string): Promise<string[]> {
  const entries = await readdir(recordsDir, { withFileTypes: true });
  const topDirs = entries
    .filter((entry) => entry.isDirectory())
    .map((entry) => join(recordsDir, entry.name))
    .sort();

  const result: string[] = [];
  for (const dir of topDirs) {
    if (hasConfigJson(dir)) {
      result.push(dir);
      continue;
    }
    const subDirs = await immediateSubDirs(dir);
    const isBatchRunDir = subDirs.some((sub) => hasConfigJson(sub));
    if (isBatchRunDir) result.push(...subDirs);
    else result.push(dir);
  }
  return result;
}
