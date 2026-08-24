import { readdir } from "node:fs/promises";
import { join } from "node:path";
import type { QueueEta } from "@bench-dashboard/shared";

// Per-queue ETA (`plans/bench-dashboard-polish.md` WP-2). Estimator input is the queue's
// own per-cell record timestamps: `runs/bakeoff/<queueStem>/<cell-dir>/config.json`'s ISO
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

export interface QueueEtaContext {
  /** `null` skips the fs read entirely (no I/O attempted) and yields an eta computed from
   * `timestamps: []` -- used when the route has no records dir configured, mirroring
   * `buildControlSection`'s "unset env -> null, no I/O" convention (`routes/queue.ts`). */
  recordsDir: string | null;
  queueStem: string;
  remainingCells: number | null;
  liveStartedAt: string | null;
}

/** The route's one fs-reading -> pure-compute seam: reads this queue's own record
 * timestamps (when `recordsDir` is set) and folds them through `computeQueueEta`. Never
 * throws -- `readQueueRecordTimestamps` itself never throws (missing dir -> `[]`, any
 * one bad cell dir skipped), so this is safe to call unconditionally per queue. */
export async function computeEtaForQueue(context: QueueEtaContext): Promise<QueueEta> {
  const timestamps = context.recordsDir === null ? [] : await readQueueRecordTimestamps(context.recordsDir, context.queueStem);
  return computeQueueEta({ timestamps, remainingCells: context.remainingCells, liveStartedAt: context.liveStartedAt });
}
