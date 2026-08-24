// Queue-tab pill/label mapping (WP-C, `bench-dashboard-run-context-surfacing.md` WP-C) --
// `QueueState`/`CellState` are `batch.py`'s own closed enums (`packages/shared/src/
// queue.ts`, mirroring `<stem>.state.json`); mapped to the vendored Signalworks pill
// classes the same way `lib/runs/verdict.ts` maps classifier verdicts (CC-04: the pill's
// own label text carries the state, color is never the sole signal). `null` covers two
// distinct "nothing to report" cases the caller keeps distinguishing -- no queue-wide state
// file yet vs. this cell's alias joining nothing in one -- both render muted/"unjoined"
// here since there's no meaningful pill-level difference between them.

import type { CellState, QueueState } from "@bench-dashboard/shared";

const QUEUE_STATE_PILL: Record<QueueState, string> = {
  running: "pill-info",
  "paused-resumable": "pill-warn",
  completed: "pill-ok",
  "aborted-hang": "pill-error",
};

export function queueStatePillClass(state: QueueState | null): string {
  return state === null ? "pill-muted" : QUEUE_STATE_PILL[state];
}

export function queueStateLabel(state: QueueState | null): string {
  return state ?? "unknown";
}

const CELL_STATE_PILL: Record<CellState, string> = {
  pending: "pill-muted",
  running: "pill-info",
  completed: "pill-ok",
  failed: "pill-error",
  "skipped-propagated": "pill-warn",
  "aborted-hang": "pill-error",
};

export function cellStatePillClass(state: CellState | null): string {
  return state === null ? "pill-muted" : CELL_STATE_PILL[state];
}

/** `null` here specifically means "unjoined" (no state file, or this cell's alias matched
 * nothing in one) -- distinct from a known state, so it gets its own label rather than
 * `queueStateLabel`'s "unknown" (there's nothing actually unknown about a queue-only cell
 * definition; it just hasn't run under a state file batch.py stamped aliases into). */
export function cellStateLabel(state: CellState | null): string {
  return state ?? "unjoined";
}
