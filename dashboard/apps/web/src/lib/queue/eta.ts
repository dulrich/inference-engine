// Queue-tab ETA line (`plans/bench-dashboard-polish.md` WP-2) -- plain, DOM-free
// formatters over the `eta` object `GET /api/queue` now carries per queue
// (`packages/shared/src/queue.ts QueueEtaSchema`), same "pure logic out of the .svelte
// file" discipline as `lib/queue/status.ts`/`lib/queue/control.ts`. Every number on the
// wire is already 1-decimal-rounded at the API (`apps/api/src/queue/eta.ts`) -- this
// module only ever formats, never re-derives.

import type { QueueEta } from "@bench-dashboard/shared";

/** `basis === null` reads as "not enough timing data yet" -- never an error state (brief's
 * hard constraint). This is the one string every queue card falls back to whenever any of
 * the pieces the full line needs (`basis`, `cellsPerHour`, `estimatedMinutesRemaining`)
 * is null, including a queue whose sibling state file doesn't exist yet at all
 * (`eta === null` on an error entry, or an `eta` object with everything still null). */
export const ETA_NO_DATA_NOTE = "Not enough timing data yet.";

/** `minutes` -> `"H h M m"` (spec pin). Rounds to the nearest whole minute before
 * splitting into hours/minutes -- the API's own 1-decimal rounding is finer than this
 * display needs. `null` (and any non-finite input) render the same em-dash `formatCount`/
 * `formatTimestamp` use elsewhere (`lib/format.ts`) for "nothing to show here". */
export function formatMinutesAsHm(minutes: number | null): string {
  if (minutes === null || !Number.isFinite(minutes)) return "—";
  const totalMinutes = Math.max(0, Math.round(minutes));
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  return `${hours} h ${mins} m`;
}

/** `basis` -> its display label (spec pin "basis labels"). `null` reads as "no data" here
 * too, though callers needing the full line fall back to `ETA_NO_DATA_NOTE` before this is
 * ever reached in practice. */
export function etaBasisLabel(basis: QueueEta["basis"]): string {
  if (basis === "current-run") return "current run";
  if (basis === "prior-runs") return "prior runs";
  return "no data yet";
}

/** The full per-queue ETA line (spec example: `"~3.2 cells/hour · est. 16 h 10 m
 * remaining (basis: current run, n=12)"`). Falls back to `ETA_NO_DATA_NOTE` whenever `eta`
 * itself is `null`/`undefined` (an error entry, or a pre-WP-2 response the wire schema's
 * `.optional()` still lets through) OR any of `basis`/`cellsPerHour`/
 * `estimatedMinutesRemaining` is null -- a queue with cell counts but no timing basis yet,
 * or one with timing data but no sibling state file to size `remainingCells` from, both
 * read as "not enough data" rather than a partial/misleading line. */
export function formatQueueEtaLine(eta: QueueEta | null | undefined): string {
  if (eta == null || eta.basis === null || eta.cellsPerHour === null || eta.estimatedMinutesRemaining === null) {
    return ETA_NO_DATA_NOTE;
  }
  const sampleSize = eta.sampleSize ?? 0;
  return `~${eta.cellsPerHour.toFixed(1)} cells/hour · est. ${formatMinutesAsHm(eta.estimatedMinutesRemaining)} remaining (basis: ${etaBasisLabel(eta.basis)}, n=${sampleSize})`;
}

/** Forward-framed complement to `formatQueueEtaLine` (spec example: `"≈5 cells in the
 * next 60 min"`) -- floors `cellsPerHour * minutes / 60`, never rounds up, so the figure
 * reads as "at least this many". Returns `null` (caller renders nothing) whenever `eta`
 * itself is `null`/`undefined` or `cellsPerHour` is null/non-finite -- same null-honest
 * discipline as the rest of this file: no rate means no estimate, never a fabricated 0. */
export function formatNextWindowEstimate(eta: QueueEta | null | undefined, minutes = 60): string | null {
  if (eta == null || eta.cellsPerHour === null || !Number.isFinite(eta.cellsPerHour)) return null;
  const cells = Math.floor((eta.cellsPerHour * minutes) / 60);
  return `≈${cells} cells in the next ${minutes} min`;
}
