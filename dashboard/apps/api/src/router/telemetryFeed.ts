import { join } from "node:path";
import type {
  RouterInFlight,
  RouterLegSummary,
  RouterProgressRecord,
  RouterRequestRecord,
  RouterTelemetryResponse,
} from "@bench-dashboard/shared";
import { RouterProgressRecordSchema, RouterRequestRecordSchema } from "@bench-dashboard/shared";
import { listDirOrNull, selectRouterTelemetryFile } from "./currentFile.ts";
import { DEFAULT_TAIL_WINDOW_BYTES, parseJsonlLines, readTailLines } from "./tailReader.ts";

// `GET /api/router/telemetry`'s reader. Takes the directory as an ARGUMENT (never
// `config.routerTelemetryDir` directly) — `config.ts` is a module-load singleton shared
// across the whole `bun test` process, so this is the same seam
// `buildQueueListResponse(queuesDir)` uses, and tests drive it with fixture dirs.

/** A progress-only `reqId` this stale (default 15 s, 15x the sink's 1000 ms progress
 * cadence) is an abandoned stream and is DROPPED from `inFlight` — the plan's "abandoned
 * stream ages out" rule. The sink never writes `stale`; it is a reader verdict. */
export const DEFAULT_TELEMETRY_STALE_MS = 15_000;

/** The newest N terminal records the response carries. The 1 MiB window holds ~558 of
 * them on a busy day (measured against the live sink), which is ~600 KB of JSON per poll —
 * bounded, but far more than a view needs. Capping here keeps `recent` and `legs`
 * consistent with each other and the response small; the WINDOW, not this cap, is what
 * bounds memory. */
export const DEFAULT_MAX_RECENT = 200;

export interface TelemetryFeedOptions {
  windowBytes?: number;
  staleMs?: number;
  maxRecent?: number;
  /** Injected clock (tests). */
  now?: Date;
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[mid] as number;
  return ((sorted[mid - 1] as number) + (sorted[mid] as number)) / 2;
}

function isErrorRecord(record: RouterRequestRecord): boolean {
  return (record.status !== null && record.status >= 400) || record.error !== null;
}

/** Per-`leg` rollup over exactly the records the response returns. The `null` leg (a
 * request that failed before target resolution) is its own bucket, never merged. */
export function summariseLegs(recent: RouterRequestRecord[]): RouterLegSummary[] {
  const byLeg = new Map<string | null, { leg: string | null; requests: number; errors: number; ttft: number[] }>();
  for (const record of recent) {
    let bucket = byLeg.get(record.leg);
    if (bucket === undefined) {
      bucket = { leg: record.leg, requests: 0, errors: 0, ttft: [] };
      byLeg.set(record.leg, bucket);
    }
    bucket.requests += 1;
    if (isErrorRecord(record)) bucket.errors += 1;
    if (record.ttftMs !== null) bucket.ttft.push(record.ttftMs);
  }
  return [...byLeg.values()]
    .map((bucket) => ({
      leg: bucket.leg,
      requests: bucket.requests,
      errors: bucket.errors,
      p50TtftMs: median(bucket.ttft),
    }))
    .sort((a, b) => b.requests - a.requests || (a.leg ?? "").localeCompare(b.leg ?? ""));
}

/** Progress lines with no terminal record for the same `reqId` in this window, minus the
 * ones that aged out. Newest-active first. */
export function deriveInFlight(
  progress: RouterProgressRecord[],
  terminalReqIds: ReadonlySet<string>,
  nowMs: number,
  staleMs: number,
): RouterInFlight[] {
  const byReq = new Map<string, RouterProgressRecord[]>();
  for (const record of progress) {
    if (terminalReqIds.has(record.reqId)) continue;
    const bucket = byReq.get(record.reqId);
    if (bucket === undefined) byReq.set(record.reqId, [record]);
    else bucket.push(record);
  }

  const inFlight: RouterInFlight[] = [];
  for (const [reqId, samples] of byReq) {
    const last = samples[samples.length - 1] as RouterProgressRecord;
    const lastSeenMs = Date.parse(last.ts);
    // An unparseable `ts` cannot be aged out honestly; treat it as stale rather than pin a
    // phantom generation to the top of the panel forever.
    if (!Number.isFinite(lastSeenMs) || nowMs - lastSeenMs > staleMs) continue;

    let tokPerSec: number | null = null;
    if (samples.length >= 2) {
      const previous = samples[samples.length - 2] as RouterProgressRecord;
      const deltaMs = last.elapsedMs - previous.elapsedMs;
      const deltaTokens =
        last.responseTokens + last.reasoningTokens - (previous.responseTokens + previous.reasoningTokens);
      if (deltaMs > 0) tokPerSec = (deltaTokens / deltaMs) * 1000;
    }

    inFlight.push({
      reqId,
      leg: last.leg,
      modelResolved: last.modelResolved,
      sessionId: last.sessionId,
      phase: last.phase,
      responseTokens: last.responseTokens,
      reasoningTokens: last.reasoningTokens,
      elapsedMs: last.elapsedMs,
      lastSeenTs: last.ts,
      tokPerSec,
    });
  }
  return inFlight.sort((a, b) => b.lastSeenTs.localeCompare(a.lastSeenTs));
}

/** `GET /api/router/telemetry`'s body. Never throws: an `undefined` dir (env unset) is
 * `disabled`, an unreadable dir is `unreadable`, and a readable dir with no sink file (or
 * a sink file holding no parseable record) is `empty`. */
export async function buildRouterTelemetryResponse(
  dir: string | undefined,
  options: TelemetryFeedOptions = {},
): Promise<RouterTelemetryResponse> {
  if (!dir) return { available: false, reason: "disabled" };

  const now = options.now ?? new Date();
  const windowBytes = options.windowBytes ?? DEFAULT_TAIL_WINDOW_BYTES;
  const staleMs = options.staleMs ?? DEFAULT_TELEMETRY_STALE_MS;
  const maxRecent = options.maxRecent ?? DEFAULT_MAX_RECENT;

  const names = await listDirOrNull(dir);
  if (names === null) return { available: false, reason: "unreadable" };

  const file = selectRouterTelemetryFile(names, now);
  if (file === null) return { available: false, reason: "empty" };

  let lines: string[];
  try {
    ({ lines } = await readTailLines(join(dir, file), windowBytes));
  } catch {
    return { available: false, reason: "unreadable" };
  }

  const requests = parseJsonlLines(lines, RouterRequestRecordSchema).records;
  const progress = parseJsonlLines(lines, RouterProgressRecordSchema).records;
  if (requests.length === 0 && progress.length === 0) return { available: false, reason: "empty" };

  const recent = requests.slice(Math.max(0, requests.length - maxRecent));
  const terminalReqIds = new Set(requests.map((record) => record.reqId));

  return {
    available: true,
    generatedAt: now.toISOString(),
    windowBytes,
    file,
    recent,
    inFlight: deriveInFlight(progress, terminalReqIds, now.getTime(), staleMs),
    legs: summariseLegs(recent),
  };
}
