// Router view (WP-4, `plans/router-observability-and-tool-search-conformance.md`) display
// formatters. Deliberately separate from `lib/format.ts`: the router plane reads as a live
// operational surface, so its durations are coarse ("3 min ago", "1.4 s") where the bench
// tables are precise ("1.42 s", a locale timestamp). Pure functions over the frozen
// `packages/shared/src/routerStatus.ts` field types -- no DOM, no clock except the caller's.

const SECOND_MS = 1000;
const MINUTE_MS = 60 * SECOND_MS;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** Coarse "N unit ago" from an already-computed age in ms. Negative ages (a writer clock
 * ahead of the reader's) clamp to 0 rather than rendering "-3 s ago" -- skew is not news. */
export function formatAgeMs(ageMs: number | null): string {
  if (ageMs === null || !Number.isFinite(ageMs)) return "—";
  const age = Math.max(0, ageMs);
  if (age < MINUTE_MS) return `${Math.floor(age / SECOND_MS)} s ago`;
  if (age < HOUR_MS) return `${Math.floor(age / MINUTE_MS)} min ago`;
  if (age < DAY_MS) return `${Math.floor(age / HOUR_MS)} h ago`;
  return `${Math.floor(age / DAY_MS)} d ago`;
}

/** Same, from a wire ISO timestamp. `now` is an injected clock so this is testable without
 * freezing time; an unparseable stamp renders the em-dash rather than "NaN s ago". */
export function formatAge(iso: string | null, now: number = Date.now()): string {
  if (iso === null) return "—";
  const parsed = new Date(iso).getTime();
  if (Number.isNaN(parsed)) return "—";
  return formatAgeMs(now - parsed);
}

/** Operational duration: one decimal in seconds past a second, whole ms below it. */
export function formatDurationMs(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return "—";
  return ms >= SECOND_MS ? `${(ms / SECOND_MS).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

/** A 0..1 ratio as a whole percentage ("62 %"); null (the contract's "not measurable yet"
 * for an idle slot, where llama-server returns no prompt counters) stays the em-dash. */
export function formatRatioPct(ratio: number | null): string {
  if (ratio === null || !Number.isFinite(ratio)) return "—";
  return `${Math.round(ratio * 100)} %`;
}

/** Ids on this surface (reqId, threadId, sessionId) are long opaque strings; the cell shows
 * a head and the full value rides along in a `title`. Anything already short passes through
 * unchanged -- a truncation that adds characters reads as broken. */
export function truncateId(value: string | null, max = 8): string {
  if (value === null) return "—";
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

/** `sessionId: null` is a real, distinguishable state on the wire (a request that carried no
 * `metadata.user_id`), not missing data -- it gets a word, not an em-dash. */
export function sessionLabel(sessionId: string | null, max = 8): string {
  return sessionId === null ? "unattributed" : truncateId(sessionId, max);
}

export function formatTokPerSec(tokPerSec: number | null): string {
  if (tokPerSec === null || !Number.isFinite(tokPerSec)) return "—";
  return `${tokPerSec.toFixed(1)} tok/s`;
}

/** Sidecar error messages are free text (a `child-stderr` line can be a whole stack); the
 * list renders exactly one line, newlines collapsed, so one bad record can't blow out the
 * panel height. */
export function truncateLine(message: string | null, max = 120): string {
  if (message === null) return "—";
  const flat = message.replace(/\s+/g, " ").trim();
  if (flat.length === 0) return "—";
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}
