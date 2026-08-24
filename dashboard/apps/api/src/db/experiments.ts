import type {
  CaptureRecord,
  ExperimentDetail,
  ExperimentSummary,
  LadderRow,
  MetricRow,
  TrialRow,
} from "@bench-dashboard/shared";
import type { NormalizedRecord } from "../ingest/record.ts";
import { db } from "./connection.ts";

// Repository seam over the four tables (`db/schema.ts`). Every function reaches `db` at
// CALL time (module-level `import { db }` binding, live ESM export) so the test-isolation
// rebind in `db/testDatabase.ts` is picked up regardless of import order — same discipline
// the model_scoreboard exemplar's `db.ts` documents.

interface ExperimentRow {
  id: string;
  schema_version: number;
  description: string | null;
  timestamp: string | null;
  hf_repo: string | null;
  hf_revision: string | null;
  filename: string | null;
  file_size: number | null;
  sha256: string | null;
  llama_cpp_build: string | null;
  sweep_cell_id: string | null;
  sweep_alias: string | null;
  sweep_server_flags: string | null;
  sweep_command: string | null;
  wilson_summary_json: string | null;
  sampling_arm: string | null;
  battery_json: string | null;
  vram_profile: string | null;
  raw_config_json: string;
  raw_prompts_json: string;
  raw_completions_json: string;
  ingested_at: string;
  trial_count?: number;
  metric_count?: number;
}

interface TrialTableRow {
  id: number;
  experiment_id: string;
  line_index: number;
  type: string;
  trial: number | null;
  sampling_arm: string | null;
  temperature: number | null;
  top_p: number | null;
  top_k: number | null;
  sampler_seed: number | null;
  haystack_seed: number | null;
  corpus_manifest_id: string | null;
  haystack_sha256: string | null;
  classifier_verdict: string | null;
  classifier_shape: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  finish_reason: string | null;
  position_fraction: number | null;
  task: string | null;
  cell_id: string | null;
  correct: number | null;
  raw_json: string;
}

interface MetricTableRow {
  id: number;
  experiment_id: string;
  line_index: number | null;
  metric_type: string;
  round_type: string | null;
  round_index: number | null;
  bytes: number | null;
  first_generation_token_ms: number | null;
  observed_decode_tok_s: number | null;
  prompt_tokens: number | null;
  cached_tokens: number | null;
  cache_hit_ratio: number | null;
  cache_assertion_passed: number | null;
  raw_json: string;
}

function rowToSummary(row: ExperimentRow): ExperimentSummary {
  return {
    id: row.id,
    schemaVersion: 2,
    description: row.description,
    timestamp: row.timestamp,
    lineage: {
      hfRepo: row.hf_repo,
      hfRevision: row.hf_revision,
      filename: row.filename,
      fileSize: row.file_size,
      sha256: row.sha256,
      llamaCppBuild: row.llama_cpp_build,
    },
    sweepCell:
      row.sweep_cell_id === null
        ? null
        : {
            cellId: row.sweep_cell_id,
            alias: row.sweep_alias ?? "",
            serverFlags: row.sweep_server_flags ? (JSON.parse(row.sweep_server_flags) as Record<string, unknown>) : {},
            command: row.sweep_command,
          },
    hasWilsonSummary: row.wilson_summary_json !== null,
    samplingArm: row.sampling_arm,
    battery: row.battery_json ? (JSON.parse(row.battery_json) as string[]) : null,
    vramProfile: row.vram_profile,
    trialCount: row.trial_count ?? 0,
    metricCount: row.metric_count ?? 0,
  };
}

function rowToTrial(row: TrialTableRow): TrialRow {
  return {
    id: row.id,
    experimentId: row.experiment_id,
    lineIndex: row.line_index,
    type: row.type,
    trial: row.trial,
    samplingArm: row.sampling_arm,
    temperature: row.temperature,
    topP: row.top_p,
    topK: row.top_k,
    samplerSeed: row.sampler_seed,
    haystackSeed: row.haystack_seed,
    corpusManifestId: row.corpus_manifest_id,
    haystackSha256: row.haystack_sha256,
    classifierVerdict: row.classifier_verdict,
    classifierShape: row.classifier_shape,
    promptTokens: row.prompt_tokens,
    completionTokens: row.completion_tokens,
    finishReason: row.finish_reason,
    positionFraction: row.position_fraction,
    task: row.task,
    cellId: row.cell_id,
    correct: row.correct === null ? null : row.correct === 1,
    raw: JSON.parse(row.raw_json) as unknown,
  };
}

function rowToMetric(row: MetricTableRow): MetricRow {
  return {
    id: row.id,
    experimentId: row.experiment_id,
    metricType: row.metric_type,
    roundType: row.round_type,
    roundIndex: row.round_index,
    bytes: row.bytes,
    firstGenerationTokenMs: row.first_generation_token_ms,
    observedDecodeTokS: row.observed_decode_tok_s,
    promptTokens: row.prompt_tokens,
    cachedTokens: row.cached_tokens,
    cacheHitRatio: row.cache_hit_ratio,
    cacheAssertionPassed: row.cache_assertion_passed === null ? null : row.cache_assertion_passed === 1,
    raw: JSON.parse(row.raw_json) as unknown,
  };
}

const SUMMARY_COLUMNS = `
  e.id, e.schema_version, e.description, e.timestamp, e.hf_repo, e.hf_revision, e.filename,
  e.file_size, e.sha256, e.llama_cpp_build, e.sweep_cell_id, e.sweep_alias,
  e.sweep_server_flags, e.sweep_command, e.wilson_summary_json, e.sampling_arm,
  e.battery_json, e.vram_profile, e.raw_config_json, e.raw_prompts_json,
  e.raw_completions_json, e.ingested_at,
  (SELECT COUNT(*) FROM trials t WHERE t.experiment_id = e.id) AS trial_count,
  (SELECT COUNT(*) FROM metrics m WHERE m.experiment_id = e.id) AS metric_count
`;

/** Idempotent write: replaces `experiments`/`trials`/`metrics`/`summaries` rows owned by
 * `record.id` in one transaction (delete-then-insert). Re-ingesting the same record
 * produces byte-identical content tables (module-boundary transaction, no partial writes
 * on a throw mid-way). */
export function replaceExperiment(record: NormalizedRecord): void {
  const tx = db.transaction((r: NormalizedRecord) => {
    db.query(`DELETE FROM experiments WHERE id = ?`).run(r.id);
    db.query(
      `
        INSERT INTO experiments (
          id, schema_version, description, timestamp, hf_repo, hf_revision, filename,
          file_size, sha256, llama_cpp_build, sweep_cell_id, sweep_alias,
          sweep_server_flags, sweep_command, wilson_summary_json, sampling_arm,
          battery_json, vram_profile, raw_config_json, raw_prompts_json,
          raw_completions_json, ingested_at
        ) VALUES (?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `,
    ).run(
      r.id,
      r.description,
      r.timestamp,
      r.hfRepo,
      r.hfRevision,
      r.filename,
      r.fileSize,
      r.sha256,
      r.llamaCppBuild,
      r.sweepCellId,
      r.sweepAlias,
      r.sweepServerFlags === null ? null : JSON.stringify(r.sweepServerFlags),
      r.sweepCommand,
      r.wilsonSummary === null ? null : JSON.stringify(r.wilsonSummary),
      r.samplingArm,
      r.battery === null ? null : JSON.stringify(r.battery),
      r.vramProfile,
      JSON.stringify(r.rawConfig),
      JSON.stringify(r.rawPrompts),
      JSON.stringify(r.rawCompletions),
      new Date().toISOString(),
    );

    const insertTrial = db.query(
      `
        INSERT INTO trials (
          experiment_id, line_index, type, trial, sampling_arm, temperature, top_p, top_k,
          sampler_seed, haystack_seed, corpus_manifest_id, haystack_sha256,
          classifier_verdict, classifier_shape, prompt_tokens, completion_tokens,
          finish_reason, position_fraction, task, cell_id, correct, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `,
    );
    for (const t of r.trials) {
      insertTrial.run(
        r.id,
        t.lineIndex,
        t.type,
        t.trial,
        t.samplingArm,
        t.temperature,
        t.topP,
        t.topK,
        t.samplerSeed,
        t.haystackSeed,
        t.corpusManifestId,
        t.haystackSha256,
        t.classifierVerdict,
        t.classifierShape,
        t.promptTokens,
        t.completionTokens,
        t.finishReason,
        t.positionFraction,
        t.task,
        t.cellId,
        t.correct === null ? null : t.correct ? 1 : 0,
        JSON.stringify(t.raw),
      );
    }

    const insertMetric = db.query(
      `
        INSERT INTO metrics (
          experiment_id, line_index, metric_type, round_type, round_index, bytes,
          first_generation_token_ms, observed_decode_tok_s, prompt_tokens, cached_tokens,
          cache_hit_ratio, cache_assertion_passed, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `,
    );
    for (const m of r.metrics) {
      insertMetric.run(
        r.id,
        m.lineIndex,
        m.metricType,
        m.roundType,
        m.roundIndex,
        m.bytes,
        m.firstGenerationTokenMs,
        m.observedDecodeTokS,
        m.promptTokens,
        m.cachedTokens,
        m.cacheHitRatio,
        m.cacheAssertionPassed === null ? null : m.cacheAssertionPassed ? 1 : 0,
        JSON.stringify(m.raw),
      );
    }

    db.query(`DELETE FROM summaries WHERE experiment_id = ?`).run(r.id);
    if (isWilsonSummaryObject(r.wilsonSummary)) {
      const w = r.wilsonSummary;
      db.query(
        `INSERT INTO summaries (experiment_id, n, k, rate, sided, confidence, lower, upper, raw_json)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ).run(
        r.id,
        numericField(w, "n"),
        numericField(w, "k"),
        numericField(w, "rate"),
        stringField(w, "sided"),
        numericField(w, "confidence"),
        numericField(w, "lower"),
        numericField(w, "upper"),
        JSON.stringify(w),
      );
    }
  });
  tx(record);
}

function isWilsonSummaryObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
function numericField(obj: Record<string, unknown>, key: string): number | null {
  const value = obj[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function stringField(obj: Record<string, unknown>, key: string): string | null {
  const value = obj[key];
  return typeof value === "string" ? value : null;
}

/** Drops every row this ingest owns across all four tables — `--rebuild`'s "drop" half.
 * `DELETE FROM experiments` alone is sufficient (every other table cascades via
 * `ON DELETE CASCADE`), but is spelled out per table for clarity under `PRAGMA foreign_keys
 * = OFF` (schema application's own window — cascade is not guaranteed there). */
export function clearAllExperiments(): void {
  const tx = db.transaction(() => {
    db.query(`DELETE FROM summaries`).run();
    db.query(`DELETE FROM metrics`).run();
    db.query(`DELETE FROM trials`).run();
    db.query(`DELETE FROM experiments`).run();
  });
  tx();
}

export function listExperimentSummaries(): ExperimentSummary[] {
  const rows = db.query(`SELECT ${SUMMARY_COLUMNS} FROM experiments e ORDER BY e.timestamp DESC, e.id ASC`).all() as ExperimentRow[];
  return rows.map(rowToSummary);
}

/** `response_caps` (the `LONGCONTEXT_MAX_TOKENS_BY_TASK` map + default cap, WP-B) has no
 * `experiments` column of its own — detail-only, parsed straight from `raw_config_json`
 * at read time rather than promoted/reconciled like `sampling_arm`/`battery_json`/
 * `vram_profile` (Decisions locked: DB is derived state, `raw_config_json` is the one
 * copy of "everything this schema doesn't name"). Absent key or non-object value -> null. */
function responseCapsFromRawConfig(rawConfigJson: string): Record<string, unknown> | null {
  const config = JSON.parse(rawConfigJson) as Record<string, unknown>;
  const caps = config.response_caps;
  return typeof caps === "object" && caps !== null && !Array.isArray(caps) ? (caps as Record<string, unknown>) : null;
}

export function getExperimentDetail(id: string): ExperimentDetail | undefined {
  const row = db.query(`SELECT ${SUMMARY_COLUMNS} FROM experiments e WHERE e.id = ?`).get(id) as ExperimentRow | null;
  if (!row) return undefined;
  return {
    ...rowToSummary(row),
    wilsonSummary: row.wilson_summary_json ? (JSON.parse(row.wilson_summary_json) as ExperimentDetail["wilsonSummary"]) : null,
    responseCaps: responseCapsFromRawConfig(row.raw_config_json),
  };
}

export function listTrials(experimentId: string): TrialRow[] {
  const rows = db
    .query(`SELECT * FROM trials WHERE experiment_id = ? ORDER BY line_index ASC`)
    .all(experimentId) as TrialTableRow[];
  return rows.map(rowToTrial);
}

export function listMetrics(experimentId: string): MetricRow[] {
  const rows = db.query(`SELECT * FROM metrics WHERE experiment_id = ? ORDER BY id ASC`).all(experimentId) as MetricTableRow[];
  return rows.map(rowToMetric);
}

export function experimentCount(): number {
  const row = db.query(`SELECT COUNT(*) AS n FROM experiments`).get() as { n: number };
  return row.n;
}

/** `GET /api/ladders/:hfRepo` support — one row per experiment sharing `hfRepo`, joining
 * the sweep-cell identity against each experiment's timing medians (`metrics`,
 * `timing_round` rows) and correctness rate (`trials`, `correct` non-null rows). Medians
 * are computed in application code (SQLite has no built-in `MEDIAN`) rather than a window
 * function, over what is expected to stay a small (tens, not thousands) per-experiment
 * row count. */
export function listLadderRows(hfRepo: string): LadderRow[] {
  const experimentRows = db
    .query(`SELECT ${SUMMARY_COLUMNS} FROM experiments e WHERE e.hf_repo = ? ORDER BY e.id ASC`)
    .all(hfRepo) as ExperimentRow[];

  return experimentRows.map((row) => {
    const flags = row.sweep_server_flags ? (JSON.parse(row.sweep_server_flags) as Record<string, unknown>) : {};
    const metricRows = db
      .query(`SELECT * FROM metrics WHERE experiment_id = ? AND metric_type = 'timing_round'`)
      .all(row.id) as MetricTableRow[];
    const trialRows = db.query(`SELECT * FROM trials WHERE experiment_id = ?`).all(row.id) as TrialTableRow[];
    const decodeSamples = metricRows.map((m) => m.observed_decode_tok_s).filter((v): v is number => v !== null);
    const ttftSamples = metricRows
      .map((m) => m.first_generation_token_ms)
      .filter((v): v is number => v !== null);
    const correctnessTrials = trialRows.filter((t) => t.correct !== null);

    return {
      experimentId: row.id,
      alias: row.sweep_alias,
      cellId: row.sweep_cell_id,
      filename: row.filename,
      contextLength: numericFlag(flags, "-c"),
      kvCacheTypeK: stringFlag(flags, "-ctk"),
      kvCacheTypeV: stringFlag(flags, "-ctv"),
      flashAttention: stringFlag(flags, "-fa"),
      medianDecodeTokS: median(decodeSamples),
      medianFirstGenerationTokenMs: median(ttftSamples),
      correctnessRate:
        correctnessTrials.length === 0
          ? null
          : correctnessTrials.filter((t) => t.correct === 1).length / correctnessTrials.length,
      trialCount: trialRows.length,
    };
  });
}

function numericFlag(flags: Record<string, unknown>, key: string): number | null {
  const value = flags[key];
  return typeof value === "number" ? value : null;
}
function stringFlag(flags: Record<string, unknown>, key: string): string | null {
  const value = flags[key];
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "on" : "off";
  return null;
}
function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? ((sorted[mid - 1] as number) + (sorted[mid] as number)) / 2 : (sorted[mid] as number);
}

/** `GET /api/curves/:experimentId` support — every `longcontext_probe` trial for the
 * experiment, grouped by `(task, position_fraction)` into one Wilson-scored point each.
 * `wilson_interval`'s own two-sided-95%-default convention is used here (this route's own
 * call, restated on every point per `curve.ts`'s `sided`/`confidence` fields — never left
 * to be assumed from context downstream). */
export function listLongcontextTrials(experimentId: string): TrialTableRow[] {
  return db
    .query(`SELECT * FROM trials WHERE experiment_id = ? AND type = 'longcontext_probe' ORDER BY line_index ASC`)
    .all(experimentId) as TrialTableRow[];
}

/** Raw record pass-through for `GET /api/export/scoreboard-capture` (experimental — see
 * `packages/shared/src/capture.ts`). Reconstructs `samples.jsonl` from `trials`+`metrics`
 * `raw_json`, ordered by `line_index`/insertion, alongside the verbatim
 * `config`/`prompts`/`completions` blobs — servable from the DB alone. */
export function getCaptureRecord(experimentId: string): CaptureRecord | undefined {
  const row = db.query(`SELECT * FROM experiments WHERE id = ?`).get(experimentId) as ExperimentRow | null;
  if (!row) return undefined;
  const trialRaws = db
    .query(`SELECT line_index, raw_json FROM trials WHERE experiment_id = ? ORDER BY line_index ASC`)
    .all(experimentId) as { line_index: number; raw_json: string }[];
  // `line_index IS NOT NULL` excludes VRAM snapshot rows (`config.json`'s `vram` object,
  // never a `samples.jsonl` line — `line_index` stays NULL for those, `db/schema.ts`).
  // Only `timing_round` metric rows carry a real `samples.jsonl` position.
  const metricRaws = db
    .query(`SELECT line_index, raw_json FROM metrics WHERE experiment_id = ? AND line_index IS NOT NULL`)
    .all(experimentId) as { line_index: number; raw_json: string }[];
  const samples = [...trialRaws, ...metricRaws]
    .sort((a, b) => a.line_index - b.line_index)
    .map((entry) => JSON.parse(entry.raw_json) as unknown);

  return {
    experimentId: row.id,
    config: JSON.parse(row.raw_config_json) as unknown,
    prompts: JSON.parse(row.raw_prompts_json) as unknown[],
    completions: JSON.parse(row.raw_completions_json) as unknown[],
    samples,
  };
}

export function listExperimentIds(): string[] {
  const rows = db.query(`SELECT id FROM experiments ORDER BY id ASC`).all() as { id: string }[];
  return rows.map((r) => r.id);
}
