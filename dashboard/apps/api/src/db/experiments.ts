import type {
  CaptureRecord,
  ExperimentDetail,
  ExperimentSummary,
  LadderRow,
  MetricRow,
  ServingAlias,
  ServingTimingRow,
  TrialRow,
} from "@bench-dashboard/shared";
import { WilsonSummarySchema } from "@bench-dashboard/shared";
import type { NormalizedRecord } from "../ingest/record.ts";
import { resolveServedModel } from "../serving/recipe.ts";
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
  // WP-1 (`plans/served-recipe-p50-prefill-and-decode.md`): reconciled columns the
  // served-recipe resolver reads (`schema.ts`) -- present on `SELECT *` already; added
  // here so `getCaptureRecord`'s `resolveServedModel` call typechecks (WP-3).
  schema_origin: string;
  model_host_path: string | null;
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
          battery_json, vram_profile, model_host_path, raw_config_json, raw_prompts_json,
          raw_completions_json, ingested_at
        ) VALUES (?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
      r.modelHostPath,
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
    // `r.wilsonSummary` is `WilsonSummary` (validated at ingest, `ingest/record.ts` --
    // `WilsonSummarySchema.safeParse`), so a non-null value here already has every field
    // in the shape/type the schema requires -- no runtime re-check needed at this point.
    if (r.wilsonSummary !== null) {
      const w = r.wilsonSummary;
      db.query(
        `INSERT INTO summaries (experiment_id, n, k, rate, sided, confidence, lower, upper, raw_json)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ).run(r.id, w.n, w.k, w.rate, w.sided, w.confidence, w.lower, w.upper, JSON.stringify(w));
    }
  });
  tx(record);
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
    // Parsed (not cast) against the same frozen schema `replaceExperiment` validated on
    // the way in -- this is our own persisted JSON, so a parse failure here means the
    // stored row itself is corrupt (bad migration, hand-edited DB, etc). That throws
    // rather than silently returning `null`: a `null` here reads as "no replicated
    // correctness cells for this experiment" -- a valid, common, and very different
    // state from "the data is broken" -- so masking corruption behind it would be a
    // worse failure than a loud 500 on this one route.
    wilsonSummary: row.wilson_summary_json ? WilsonSummarySchema.parse(JSON.parse(row.wilson_summary_json)) : null,
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
/** Linear-interpolation percentile at rank `p·(n−1)` over the sorted sample (WP-1,
 * `plans/served-recipe-p50-prefill-and-decode.md`, generalizing the pre-WP-1 `median`).
 * `n=1` returns the sample's one value for every `p` (Decisions locked) -- handled as its
 * own branch rather than falling out of the general formula by coincidence, since
 * `p·(n−1)` is `0` for every `p` when `n=1` regardless, but writing it out keeps the
 * "single sample, any percentile is that sample" invariant readable at the call site.
 * `percentile(values, 0.5) === ` the old `median` byte-for-byte (locked by test) -- every
 * existing caller (`listLadderRows` above) passes `0.5` and is unaffected. */
export function percentile(values: number[], p: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  if (sorted.length === 1) return sorted[0] as number;
  const rank = p * (sorted.length - 1);
  const lower = Math.floor(rank);
  const upper = Math.ceil(rank);
  if (lower === upper) return sorted[lower] as number;
  const weight = rank - lower;
  return (sorted[lower] as number) * (1 - weight) + (sorted[upper] as number) * weight;
}
function median(values: number[]): number | null {
  return percentile(values, 0.5);
}

interface ServingTimingSourceRow {
  model_host_path: string | null;
  sweep_alias: string | null;
  schema_origin: string;
  sampling_arm: string | null;
  sweep_server_flags: string | null;
  observed_decode_tok_s: number | null;
  observed_prefill_tok_s: number | null;
}

interface ServingTimingRound {
  decode: number | null;
  prefill: number | null;
}

interface ServingTimingGroup {
  modelKey: string;
  aliases: string[];
  mtp: boolean;
  byArm: Map<string | null, ServingTimingRound[]>;
}

/** Arm preference (Decisions locked, unchanged from `bakeoff-timing-records-and-estimates.
 * md`): `production` -> `capability` -> unlabelled-only. `byArm` is never empty when this
 * is called (`listServingTimingRows` only creates a group entry once it has at least one
 * round). The final fallback (whatever arm key IS present) covers "unlabelled-only" --
 * `sampling_arm IS NULL` groups under the `null` key -- and, defensively, any future arm
 * value this fleet hasn't introduced yet, so a group is never dropped silently for want of
 * a name this function doesn't recognize. */
function pickArm(byArm: Map<string | null, ServingTimingRound[]>): string | null {
  if (byArm.has("production")) return "production";
  if (byArm.has("capability")) return "capability";
  const [first] = byArm.keys();
  return first ?? null;
}

/** `GET /api/serving/timing` support (WP-1, revising WP-3's original alias-keyed
 * aggregate) -- p50/p90 prefill + decode tok/s, grouped by SERVED MODEL via the recipe
 * resolver (`serving/recipe.ts resolveServedModel`), never by raw `sweep_alias` directly.
 * Selects every `timing_round` metric (not pre-filtered to a non-null decode sample --
 * prefill's own nullability is independent, "prefill samples may be fewer"), resolves each
 * row against `servedAliases` (`GET /api/serving`'s own parse, passed in by the route --
 * this function never opens `llama/config.yaml` itself), and keeps only `recipeMatch ===
 * true` rows ("only the currently-served recipe contributes to a model's Serving row").
 * Rounds are bucketed by `(modelKey, samplingArm)`; `pickArm` picks ONE arm per model
 * (arm-aware, never pooled -- the winning arm supplies both prefill and decode together);
 * `percentile` computes p50/p90 over each metric's own non-null samples within that arm. A
 * served model with zero admissible rounds across every arm never gets a `Map` entry at
 * all, so it simply doesn't appear in the output -- "zero admissible rounds ⇒ no row,
 * never a computed zero". */
export function listServingTimingRows(servedAliases: readonly ServingAlias[]): ServingTimingRow[] {
  const sourceRows = db
    .query(
      `
        SELECT
          e.model_host_path AS model_host_path,
          e.sweep_alias AS sweep_alias,
          e.schema_origin AS schema_origin,
          e.sampling_arm AS sampling_arm,
          e.sweep_server_flags AS sweep_server_flags,
          m.observed_decode_tok_s AS observed_decode_tok_s,
          json_extract(m.raw_json, '$.server_prompt_eval_tok_s') AS observed_prefill_tok_s
        FROM metrics m
        JOIN experiments e ON e.id = m.experiment_id
        WHERE m.metric_type = 'timing_round'
      `,
    )
    .all() as ServingTimingSourceRow[];

  const groups = new Map<string, ServingTimingGroup>();
  for (const row of sourceRows) {
    const resolved = resolveServedModel(
      {
        modelHostPath: row.model_host_path,
        sweepAlias: row.sweep_alias,
        schemaOrigin: row.schema_origin,
        sweepServerFlags: row.sweep_server_flags,
      },
      servedAliases,
    );
    if (resolved === null || !resolved.recipeMatch) continue;

    let group = groups.get(resolved.modelKey);
    if (!group) {
      group = { modelKey: resolved.modelKey, aliases: resolved.aliases, mtp: resolved.mtp, byArm: new Map() };
      groups.set(resolved.modelKey, group);
    }
    const rounds = group.byArm.get(row.sampling_arm);
    const round: ServingTimingRound = { decode: row.observed_decode_tok_s, prefill: row.observed_prefill_tok_s };
    if (rounds) rounds.push(round);
    else group.byArm.set(row.sampling_arm, [round]);
  }

  const rows: ServingTimingRow[] = [];
  for (const group of groups.values()) {
    const arm = pickArm(group.byArm);
    const rounds = group.byArm.get(arm) ?? [];
    if (rounds.length === 0) continue;
    const decodeSamples = rounds.map((r) => r.decode).filter((v): v is number => v !== null);
    const prefillSamples = rounds.map((r) => r.prefill).filter((v): v is number => v !== null);
    rows.push({
      modelKey: group.modelKey,
      aliases: group.aliases,
      p50PrefillTokS: percentile(prefillSamples, 0.5),
      p90PrefillTokS: percentile(prefillSamples, 0.9),
      p50DecodeTokS: percentile(decodeSamples, 0.5),
      p90DecodeTokS: percentile(decodeSamples, 0.9),
      samplingArm: arm,
      sampleCount: rounds.length,
      mtp: group.mtp,
    });
  }

  return rows.sort((a, b) => a.modelKey.localeCompare(b.modelKey));
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
 * `config`/`prompts`/`completions` blobs — servable from the DB alone.
 *
 * `servedAliases` (WP-3, `plans/served-recipe-p50-prefill-and-decode.md`) is `GET
 * /api/serving`'s own parse, passed in by `routes/export.ts` (read ONCE per request, same
 * pattern as `routes/servingTiming.ts` -- this function never opens `llama/config.yaml`
 * itself). `serving` is computed by the SAME resolver the Serving view uses
 * (`serving/recipe.ts resolveServedModel`) over the row's own reconciled columns
 * (`model_host_path`, `sweep_alias`, `schema_origin`, `sweep_server_flags`) -- never
 * `raw_config_json` -- and is `null`, never a guessed object, when the row resolves to no
 * served model. Every verbatim field below (`config`/`prompts`/`completions`/`samples`)
 * stays byte-for-byte unchanged; `serving` is a new, additive sibling key only. */
export function getCaptureRecord(experimentId: string, servedAliases: readonly ServingAlias[]): CaptureRecord | undefined {
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

  const serving = resolveServedModel(
    {
      modelHostPath: row.model_host_path,
      sweepAlias: row.sweep_alias,
      schemaOrigin: row.schema_origin,
      sweepServerFlags: row.sweep_server_flags,
    },
    servedAliases,
  );

  return {
    experimentId: row.id,
    config: JSON.parse(row.raw_config_json) as unknown,
    prompts: JSON.parse(row.raw_prompts_json) as unknown[],
    completions: JSON.parse(row.raw_completions_json) as unknown[],
    samples,
    serving,
  };
}

export function listExperimentIds(): string[] {
  const rows = db.query(`SELECT id FROM experiments ORDER BY id ASC`).all() as { id: string }[];
  return rows.map((r) => r.id);
}
