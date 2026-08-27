import { readdir } from "node:fs/promises";
import { join } from "node:path";
import type { CellState, QueueEta } from "@bench-dashboard/shared";

// Per-queue ETA (`plans/bench-dashboard-polish.md` WP-2). Estimator input is the queue's
// own per-cell record timestamps: `llama/bakeoff/records/<queueStem>/<cell-dir>/config.json`'s ISO
// `timestamp` field (measured against the live run 2026-08-24 -- one cell dir per
// completed/in-flight cell, records accumulate in the same dir across nights). This module
// only ever READS `recordsDir` (the records mount is `:ro` in production) -- never a write
// path. The browser never touches the filesystem; every field below is computed here and
// carried on the wire as an additive `eta` object (`packages/shared/src/queue.ts`).

/** A delta above this is a gap between nightly windows, not real cell wall time (spec
 * pin, measured against the live ladder-v1 run 2026-08-24: within-window cell deltas are
 * on the order of minutes, between-window gaps are hours). */
const MAX_RETAINED_DELTA_SECONDS = 3600;

/** `estimatedMinutesRemaining`/`medianCellMinutes`/`cellsPerHour` are all rounded to one
 * decimal place at the API (spec pin) -- the wire never carries raw floating-point noise
 * from a `Date.parse` millisecond difference. */
function round1(value: number): number {
  return Math.round(value * 10) / 10;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Reads every `<recordsDir>/<queueStem>/<cell-dir>/config.json`'s `timestamp` field --
 * one level of cell subdirectories under the queue stem, the measured real layout, never a
 * general fs walk. A missing queue dir returns `[]` (a queue that hasn't produced any
 * records yet, or hasn't run under this stem name, is "no data", not an error). Within
 * that, an unreadable cell dir, invalid JSON, or a non-string/absent `timestamp` is
 * skipped for that ONE cell -- never fatal to the rest of the scan (mirrors the queue
 * route's own malformed-file philosophy, `routes/queue.ts`). */
export async function readQueueRecordTimestamps(recordsDir: string, queueStem: string): Promise<string[]> {
  const queueDir = join(recordsDir, queueStem);
  let cellDirNames: string[];
  try {
    const entries = await readdir(queueDir, { withFileTypes: true });
    cellDirNames = entries
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort();
  } catch {
    return [];
  }

  const timestamps: string[] = [];
  for (const cellDirName of cellDirNames) {
    try {
      const text = await Bun.file(join(queueDir, cellDirName, "config.json")).text();
      const parsed = JSON.parse(text) as unknown;
      if (isRecord(parsed) && typeof parsed.timestamp === "string") {
        timestamps.push(parsed.timestamp);
      }
    } catch {
      // Unreadable/missing config.json, or invalid JSON, for this one cell -- skipped,
      // never aborts the scan (a cell mid-write, or one that errored before its config
      // landed, is an expected, common state).
    }
  }
  return timestamps;
}

export interface CellTimeStats {
  /** Median of the retained consecutive-timestamp deltas, in minutes. `null` when zero
   * deltas were retained (spec step 3: "No retained deltas ⇒ everything null"). */
  medianCellMinutes: number | null;
  /** Retained-delta count. `null` (not `0`) alongside `medianCellMinutes: null` -- this is
   * itself one of the null-honest ETA numbers, never a bare zero standing in for "no
   * basis". */
  sampleSize: number | null;
  /** Both endpoint timestamps of every retained delta -- the records that actually
   * contributed to `medianCellMinutes`, carried through so `resolveBasis` can compare them
   * against the live window's `startedAt` without re-deriving them. */
  contributingTimestamps: string[];
}

/** Sorts `timestamps` ascending, computes consecutive deltas, and retains only deltas
 * `<= MAX_RETAINED_DELTA_SECONDS` (spec step 2: a bigger gap is idle time between nightly
 * windows, not one cell's wall time). Unparseable timestamps are dropped before sorting --
 * same "skip, never fatal" discipline as the fs-reading wrapper above. */
export function computeCellTimeStats(timestamps: string[]): CellTimeStats {
  const parsed = timestamps
    .map((raw) => ({ raw, ms: Date.parse(raw) }))
    .filter((entry): entry is { raw: string; ms: number } => Number.isFinite(entry.ms))
    .sort((a, b) => a.ms - b.ms);

  const retained: { deltaMinutes: number; earlierRaw: string; laterRaw: string }[] = [];
  for (let i = 1; i < parsed.length; i++) {
    // `noUncheckedIndexedAccess`: both indices are always in-bounds here (loop starts at
    // 1, `i - 1` is always `>= 0`) -- the `undefined` checks are never actually hit, just
    // satisfying the compiler the same way `ingest/cli.ts`'s argv-index checks do.
    const earlier = parsed[i - 1];
    const later = parsed[i];
    if (earlier === undefined || later === undefined) continue;
    const deltaSeconds = (later.ms - earlier.ms) / 1000;
    if (deltaSeconds <= MAX_RETAINED_DELTA_SECONDS) {
      retained.push({ deltaMinutes: deltaSeconds / 60, earlierRaw: earlier.raw, laterRaw: later.raw });
    }
  }

  if (retained.length === 0) {
    return { medianCellMinutes: null, sampleSize: null, contributingTimestamps: [] };
  }

  const sortedDeltas = [...retained].sort((a, b) => a.deltaMinutes - b.deltaMinutes);
  const mid = Math.floor(sortedDeltas.length / 2);
  // Both indices are in-bounds by construction (`retained.length >= 1` was just checked
  // above, and `mid`/`mid - 1` are both valid indices into a same-length array) -- same
  // "never actually hit" compiler-satisfying guard as above.
  const midValue = sortedDeltas[mid];
  const beforeMidValue = sortedDeltas[mid - 1];
  if (midValue === undefined || (sortedDeltas.length % 2 === 0 && beforeMidValue === undefined)) {
    return { medianCellMinutes: null, sampleSize: null, contributingTimestamps: [] };
  }
  const medianCellMinutes =
    sortedDeltas.length % 2 === 0 && beforeMidValue !== undefined ? (beforeMidValue.deltaMinutes + midValue.deltaMinutes) / 2 : midValue.deltaMinutes;

  const contributingTimestamps: string[] = [];
  for (const entry of retained) {
    contributingTimestamps.push(entry.earlierRaw, entry.laterRaw);
  }

  return { medianCellMinutes, sampleSize: retained.length, contributingTimestamps };
}

/** `"current-run"` when any contributing record's timestamp is `>=` the live window's
 * `startedAt` (spec step 4), `"prior-runs"` when there's data but none of it is that
 * recent, `null` when there's no contributing data at all. `liveStartedAt` is `null`
 * whenever this queue is not the one `control.live` currently names -- callers only pass a
 * real value for the queue that IS live right now. */
export function resolveBasis(contributingTimestamps: string[], liveStartedAt: string | null): "current-run" | "prior-runs" | null {
  if (contributingTimestamps.length === 0) return null;
  if (liveStartedAt === null) return "prior-runs";

  const liveMs = Date.parse(liveStartedAt);
  if (!Number.isFinite(liveMs)) return "prior-runs";

  const isCurrentRun = contributingTimestamps.some((raw) => {
    const ms = Date.parse(raw);
    return Number.isFinite(ms) && ms >= liveMs;
  });
  return isCurrentRun ? "current-run" : "prior-runs";
}

export interface ComputeQueueEtaInput {
  timestamps: string[];
  /** `cellCounts.pending + cellCounts.running`, already computed by the route -- `null`
   * when there's no sibling state file at all (spec step 5). */
  remainingCells: number | null;
  /** `control.live.startedAt` when `control.live` is non-null AND names this queue,
   * otherwise `null` (spec step 4). */
  liveStartedAt: string | null;
}

/** The pure core: `timestamps` + the route's own `remainingCells`/`liveStartedAt` in,
 * a fully-populated `QueueEta` out (spec steps 2-5). Never throws -- every input shape,
 * including an empty `timestamps` array, produces a valid (possibly all-timing-null)
 * result. */
export function computeQueueEta(input: ComputeQueueEtaInput): QueueEta {
  const { medianCellMinutes, sampleSize, contributingTimestamps } = computeCellTimeStats(input.timestamps);
  const basis = resolveBasis(contributingTimestamps, input.liveStartedAt);

  const estimatedMinutesRemaining =
    medianCellMinutes !== null && input.remainingCells !== null ? round1(input.remainingCells * medianCellMinutes) : null;
  const cellsPerHour = medianCellMinutes !== null && medianCellMinutes > 0 ? round1(60 / medianCellMinutes) : null;

  return {
    remainingCells: input.remainingCells,
    medianCellMinutes: medianCellMinutes !== null ? round1(medianCellMinutes) : null,
    estimatedMinutesRemaining,
    cellsPerHour,
    basis,
    sampleSize,
  };
}

// ---- Model/context-aware cost table (this plan's WP-2, `plans/bakeoff-timing-records-
// and-estimates.md`) -- a per-(model, context) cost table built from the SAME queue's own
// on-disk cell records, keyed by `sweep_cell.model` (WP-1's identity pin; absent on every
// record written before it lands) and the record's own `-c` server flag (already present
// today). A record missing either half of that identity contributes nothing to the table,
// which is exactly why every record on disk today (all pre-WP-1) leaves the table empty
// and the estimator below falls through to the untouched legacy median -- "byte-identical
// when no better basis exists" (hard constraint) falls out of this by construction, not a
// special case.

export interface CostSample {
  model: string;
  context: number;
  /** Seconds. Prefers `cell_wall_seconds` (WP-1, a direct measured float), then a
   * `cell_started_at`/`cell_ended_at` diff, then this cell's own summed probe
   * `wall_seconds` -- the last of which UNDERSTATES true wall time (excludes model
   * load/teardown). Still classified as an "exact (model, context)" sample regardless of
   * which of the three produced it: the scaling-caveat accounting below keys off which
   * FALLBACK TIER resolves a given remaining cell, never off how one sample was itself
   * measured (Scope detail #1's "carry that as part of the scaling caveat, not silently"
   * -- a queue whose only data is probe-summed still flows through the same caveat
   * machinery the moment it has to project across contexts; it is never a second, hidden
   * discount). */
  costSeconds: number;
  /** Average `prompt_tokens` across this cell's own `longcontext_probe` rows -- `null`
   * when `samples.jsonl` is missing/unreadable or has no rows with a numeric value. Needed
   * only by the same-model-any-context scaling tier; an exact-bucket sample with `null`
   * here still contributes fully to that bucket's own median cost. */
  avgPromptTokens: number | null;
}

function extractRecordModel(config: Record<string, unknown>): string | null {
  const sweepCell = config.sweep_cell;
  if (!isRecord(sweepCell)) return null;
  return typeof sweepCell.model === "string" ? sweepCell.model : null;
}

/** The record's own context length -- `sweep_cell.server_flags["-c"]`, present on every
 * schema-v2 record today (unlike `sweep_cell.model`, which WP-1 adds). This is the SAME
 * raw number the queue file's own cell `c` field carries (both ultimately come from the
 * queue definition that spawned the cell), so an exact-bucket join is a plain `===`. */
function extractRecordContext(config: Record<string, unknown>): number | null {
  const sweepCell = config.sweep_cell;
  if (!isRecord(sweepCell)) return null;
  const serverFlags = sweepCell.server_flags;
  if (!isRecord(serverFlags)) return null;
  const c = serverFlags["-c"];
  return typeof c === "number" && Number.isFinite(c) ? c : null;
}

function extractRecordWallSeconds(config: Record<string, unknown>): number | null {
  const value = config.cell_wall_seconds;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function extractRecordTimestampDiffSeconds(config: Record<string, unknown>): number | null {
  const startedRaw = config.cell_started_at;
  const endedRaw = config.cell_ended_at;
  if (typeof startedRaw !== "string" || typeof endedRaw !== "string") return null;
  const started = Date.parse(startedRaw);
  const ended = Date.parse(endedRaw);
  if (!Number.isFinite(started) || !Number.isFinite(ended) || ended < started) return null;
  return (ended - started) / 1000;
}

interface ProbeMetrics {
  /** Sum of every `longcontext_probe` row's `wall_seconds` -- the last-resort per-cell
   * cost, used only when neither `cell_wall_seconds` nor a `cell_started_at`/`cell_ended_at`
   * pair is present. `null` when the file is missing/unreadable or has zero valid rows. */
  summedWallSeconds: number | null;
  avgPromptTokens: number | null;
}

/** Reads `<cellDir>/samples.jsonl` once for both figures a cost sample can need. One bad
 * line (invalid JSON, wrong shape) is skipped -- never aborts the rest of the file, same
 * discipline as every other per-record reader in this module. */
async function readCellProbeMetrics(cellDir: string): Promise<ProbeMetrics> {
  let text: string;
  try {
    text = await Bun.file(join(cellDir, "samples.jsonl")).text();
  } catch {
    return { summedWallSeconds: null, avgPromptTokens: null };
  }

  let wallTotal = 0;
  let wallCount = 0;
  let tokenTotal = 0;
  let tokenCount = 0;
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (trimmed === "") continue;
    let row: unknown;
    try {
      row = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (!isRecord(row) || row.type !== "longcontext_probe") continue;
    if (typeof row.wall_seconds === "number" && Number.isFinite(row.wall_seconds)) {
      wallTotal += row.wall_seconds;
      wallCount += 1;
    }
    if (typeof row.prompt_tokens === "number" && Number.isFinite(row.prompt_tokens) && row.prompt_tokens > 0) {
      tokenTotal += row.prompt_tokens;
      tokenCount += 1;
    }
  }

  return {
    summedWallSeconds: wallCount > 0 ? wallTotal : null,
    avgPromptTokens: tokenCount > 0 ? tokenTotal / tokenCount : null,
  };
}

/** One `CostSample` per cell dir under `<recordsDir>/<queueStem>/` that carries a full
 * (model, context) identity -- a record missing `sweep_cell.model` (every record today) or
 * a numeric `-c` flag is dropped from the table entirely, never guessed. Never throws --
 * one unreadable/malformed cell dir contributes nothing and the scan continues, exactly
 * `readQueueRecordTimestamps`'s own "skip, never fatal" contract. */
export async function readQueueCostSamples(recordsDir: string, queueStem: string): Promise<CostSample[]> {
  const queueDir = join(recordsDir, queueStem);
  let cellDirNames: string[];
  try {
    const entries = await readdir(queueDir, { withFileTypes: true });
    cellDirNames = entries
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort();
  } catch {
    return [];
  }

  const samples: CostSample[] = [];
  for (const cellDirName of cellDirNames) {
    const cellDir = join(queueDir, cellDirName);
    try {
      const text = await Bun.file(join(cellDir, "config.json")).text();
      const parsed = JSON.parse(text) as unknown;
      if (!isRecord(parsed)) continue;

      const model = extractRecordModel(parsed);
      const context = extractRecordContext(parsed);
      if (model === null || context === null) continue;

      const probeMetrics = await readCellProbeMetrics(cellDir);
      const costSeconds = extractRecordWallSeconds(parsed) ?? extractRecordTimestampDiffSeconds(parsed) ?? probeMetrics.summedWallSeconds;
      if (costSeconds === null) continue;

      samples.push({ model, context, costSeconds, avgPromptTokens: probeMetrics.avgPromptTokens });
    } catch {
      // Unreadable/missing config.json, or invalid JSON, for this one cell -- skipped.
    }
  }
  return samples;
}

function medianOf(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  const midValue = sorted[mid];
  const beforeMidValue = sorted[mid - 1];
  if (midValue === undefined) return null;
  if (sorted.length % 2 === 0 && beforeMidValue !== undefined) return (beforeMidValue + midValue) / 2;
  return midValue;
}

interface CostTable {
  exact: Map<string, CostSample[]>;
  byModel: Map<string, CostSample[]>;
}

function exactBucketKey(model: string, context: number): string {
  return `${model}::${context}`;
}

function buildCostTable(samples: CostSample[]): CostTable {
  const exact = new Map<string, CostSample[]>();
  const byModel = new Map<string, CostSample[]>();
  for (const sample of samples) {
    const key = exactBucketKey(sample.model, sample.context);
    const exactBucket = exact.get(key);
    if (exactBucket) exactBucket.push(sample);
    else exact.set(key, [sample]);

    const modelBucket = byModel.get(sample.model);
    if (modelBucket) modelBucket.push(sample);
    else byModel.set(sample.model, [sample]);
  }
  return { exact, byModel };
}

interface TierResolution {
  minutes: number;
  sampleCount: number;
}

/** Fallback-chain tier 1: the median cost of every sample at this EXACT (model, context)
 * bucket -- the discrete `c` value itself IS the bucket (audit E2 disposition: no ranges,
 * ladder-v2 cells use exact powers of two). */
function resolveExactTier(table: CostTable, model: string, context: number): TierResolution | null {
  const samples = table.exact.get(exactBucketKey(model, context));
  if (!samples || samples.length === 0) return null;
  const median = medianOf(samples.map((s) => s.costSeconds));
  if (median === null) return null;
  return { minutes: median / 60, sampleCount: samples.length };
}

/** Fallback-chain tier 2: same model, any OTHER context -- builds one seconds-per-
 * prompt-token rate (the median of each sample's own `costSeconds / avgPromptTokens`) from
 * every same-model sample that has prompt-token data, then projects it onto the remaining
 * cell's own `c` as a proxy for its not-yet-run prompt token count (needle probes target
 * close to the full context window). This is a LINEAR projection over a known-superlinear
 * prefill cost curve, so it UNDERESTIMATES larger contexts -- `scalingCaveat` exists to
 * surface exactly that, never to hide it. */
function resolveScaledTier(table: CostTable, model: string, context: number): TierResolution | null {
  const candidates = table.byModel.get(model) ?? [];
  const samples = candidates.filter((s) => s.avgPromptTokens !== null && s.avgPromptTokens > 0);
  if (samples.length === 0) return null;
  const rate = medianOf(samples.map((s) => s.costSeconds / (s.avgPromptTokens as number)));
  if (rate === null || !Number.isFinite(rate)) return null;
  return { minutes: (rate * context) / 60, sampleCount: samples.length };
}

export interface CellDescriptor {
  alias: string;
  model: string | null;
  c: number | null;
  state: CellState | null;
}

/** Fallback-chain tier 3 IS `legacy.medianCellMinutes` -- the exact same number today's
 * global-median estimator already produces, so a cell with no model-table basis at all
 * lands on today's behavior for that one cell, never a guess. Returns `legacy` completely
 * unchanged -- object identity included -- whenever zero remaining descriptors resolve via
 * tier 1/2 (e.g. an empty cost table, which is every record on disk today, all lacking
 * `sweep_cell.model`): no `bucketSampleSize`/`scalingCaveat` key is ever added in that
 * case, so the wire JSON is byte-identical to today's, not merely value-equal (hard
 * constraint). `remainingCells`/`sampleSize` pass through from `legacy` untouched in every
 * case -- a cell COUNT and a timestamp-delta count, both orthogonal to which basis produced
 * the timing numbers. */
function applyModelTableEta(legacy: QueueEta, costSamples: CostSample[], descriptors: CellDescriptor[]): QueueEta {
  const remaining = descriptors.filter((d) => d.state === "pending" || d.state === "running");
  if (remaining.length === 0 || costSamples.length === 0) return legacy;

  const table = buildCostTable(costSamples);
  const legacyMinutes = legacy.medianCellMinutes;

  let usedModelTable = false;
  let usedNonExact = false;
  let bucketSampleSize = 0;
  const resolvedMinutes: number[] = [];

  for (const cell of remaining) {
    const exact = cell.model !== null && cell.c !== null ? resolveExactTier(table, cell.model, cell.c) : null;
    if (exact !== null) {
      usedModelTable = true;
      bucketSampleSize += exact.sampleCount;
      resolvedMinutes.push(exact.minutes);
      continue;
    }

    const scaled = cell.model !== null && cell.c !== null ? resolveScaledTier(table, cell.model, cell.c) : null;
    if (scaled !== null) {
      usedModelTable = true;
      usedNonExact = true;
      bucketSampleSize += scaled.sampleCount;
      resolvedMinutes.push(scaled.minutes);
      continue;
    }

    if (legacyMinutes !== null) {
      usedNonExact = true;
      resolvedMinutes.push(legacyMinutes);
      continue;
    }

    // No basis at all for this one cell (no exact/scaled sample AND no legacy median) --
    // excluded from the sum entirely, never fabricated. Flipping `usedNonExact` here too
    // means a queue that had to drop a cell this way is never reported as purely-exact.
    usedNonExact = true;
  }

  if (!usedModelTable) return legacy;

  const medianMinutes = medianOf(resolvedMinutes);
  const totalMinutes = resolvedMinutes.reduce((sum, minutes) => sum + minutes, 0);

  return {
    ...legacy,
    basis: "model-table",
    medianCellMinutes: medianMinutes !== null ? round1(medianMinutes) : null,
    estimatedMinutesRemaining: round1(totalMinutes),
    cellsPerHour: medianMinutes !== null && medianMinutes > 0 ? round1(60 / medianMinutes) : null,
    bucketSampleSize,
    scalingCaveat: usedNonExact,
  };
}

export interface QueueEtaContext {
  /** `null` skips the fs read entirely (no I/O attempted) and yields an eta computed from
   * `timestamps: []` -- used when the route has no records dir configured, mirroring
   * `buildControlSection`'s "unset env -> null, no I/O" convention (`routes/queue.ts`). */
  recordsDir: string | null;
  queueStem: string;
  remainingCells: number | null;
  liveStartedAt: string | null;
  /** Every cell this queue's OWN queue file declares, any state (`routes/queue.ts`'s
   * already-built `cells` array, `:203-217`, passed straight through) -- `computeEtaForQueue`
   * filters to `state === "pending" | "running"` itself, never the caller. `undefined`
   * (every pre-WP-2 call site, including this module's own pre-WP-2 tests) behaves exactly
   * like `[]`: no descriptors means no possible model-table basis, so only the untouched
   * legacy computation ever runs -- WP-2's "byte-identical when no better basis exists"
   * hard constraint. */
  cellDescriptors?: CellDescriptor[];
}

/** The route's one fs-reading -> pure-compute seam: reads this queue's own record
 * timestamps (when `recordsDir` is set) and folds them through `computeQueueEta` for the
 * legacy global-median baseline, then (WP-2) attempts to sharpen that baseline with a
 * model/context-aware cost table built from the same queue's own records -- `legacy` is
 * returned completely unchanged whenever that sharpening finds nothing to add (no
 * descriptors, no records dir, or an empty cost table), which is what makes "byte-identical
 * when no better basis exists" true at the JSON level, not just the asserted-value level.
 * Never throws -- every reader this calls has its own "skip, never fatal" contract. */
export async function computeEtaForQueue(context: QueueEtaContext): Promise<QueueEta> {
  const timestamps = context.recordsDir === null ? [] : await readQueueRecordTimestamps(context.recordsDir, context.queueStem);
  const legacy = computeQueueEta({ timestamps, remainingCells: context.remainingCells, liveStartedAt: context.liveStartedAt });

  const descriptors = context.cellDescriptors ?? [];
  if (context.recordsDir === null || descriptors.length === 0) return legacy;

  const costSamples = await readQueueCostSamples(context.recordsDir, context.queueStem);
  return applyModelTableEta(legacy, costSamples, descriptors);
}
