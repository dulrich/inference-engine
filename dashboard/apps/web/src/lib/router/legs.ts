// Legs & sidecar panel logic (WP-4). The wire delivers `recent` in FILE order (oldest ->
// newest, `packages/shared/src/routerStatus.ts`) and `legs` as a rollup over exactly that
// capped set -- so the newest-first reversal happens here, ONCE, rather than in the markup,
// and the filter is applied to the already-reversed list.

import type { RouterLegSummary, RouterRequestRecord } from "@bench-dashboard/shared";
import { formatDurationMs } from "./format.ts";

/** The seg-group's "no filter" segment key. Not a leg name -- the sink's leg values are
 * gateway route names (`claude-local`, `codex-b`, ...), never this. */
export const ALL_LEGS = "all";

/** `leg: null` is the unattributed bucket (a request that failed before target resolution),
 * a real state with its own seg segment -- so it needs a stable non-null key. */
export const UNATTRIBUTED_LEG = "unattributed";

export function legKey(leg: string | null): string {
  return leg ?? UNATTRIBUTED_LEG;
}

/** Newest first, on a COPY -- `recent` is reactive state the panel keeps across polls, and
 * an in-place `.reverse()` would flip it again on every re-render. */
export function newestFirst(recent: RouterRequestRecord[]): RouterRequestRecord[] {
  return recent.slice().reverse();
}

/** The contract's own error predicate, identical to the one `legs[].errors` counts with
 * (`RouterLegSummarySchema`): a 4xx/5xx status OR a non-null error envelope. A record still
 * in flight has `status: null` and is not an error. */
export function isErrorRecord(record: RouterRequestRecord): boolean {
  return (record.status !== null && record.status >= 400) || record.error != null;
}

export function applyLegFilter(records: RouterRequestRecord[], filter: string): RouterRequestRecord[] {
  return filter === ALL_LEGS ? records : records.filter((record) => legKey(record.leg) === filter);
}

export interface LegChip {
  key: string;
  label: string;
  requests: number;
  errors: number;
  p50Label: string;
  /** CC-04 redundancy: the chip's own text already says "N errors"; this only picks the
   * pill class, never the only carrier. */
  pillClass: string;
}

export function legChips(legs: RouterLegSummary[]): LegChip[] {
  return legs.map((summary) => ({
    key: legKey(summary.leg),
    label: legKey(summary.leg),
    requests: summary.requests,
    errors: summary.errors,
    p50Label: formatDurationMs(summary.p50TtftMs),
    pillClass: summary.errors > 0 ? "pill-error" : "pill-muted",
  }));
}

/** Seg-group options: "all" first, then one per leg the response actually reported, in the
 * response's own order (the API groups them; the view never invents a leg the sink hasn't
 * seen in this window). */
export function legFilterOptions(legs: RouterLegSummary[]): string[] {
  const keys = legs.map((summary) => legKey(summary.leg));
  return [ALL_LEGS, ...keys.filter((key, index) => keys.indexOf(key) === index)];
}

/** A filter can outlive the leg it names (the window rolls, the leg goes quiet) -- fall back
 * to "all" rather than rendering an empty table under a segment nothing can select away
 * from. */
export function resolveLegFilter(filter: string, options: string[]): string {
  return options.includes(filter) ? filter : ALL_LEGS;
}
