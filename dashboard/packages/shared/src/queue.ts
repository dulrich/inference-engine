import { z } from "zod";
import { ControlSectionSchema } from "./control.ts";

// `GET /api/queue` (WP-A, `plans/bench-dashboard-run-context-surfacing.md` WP-A) — a
// read-only view over `llama/bakeoff/queues/*.json` (queue definitions) joined against
// their sibling `<stem>.state.json` (batch.py's live/terminal run state) BY ALIAS
// (Decisions locked: "no TypeScript reimplementation of the Python cell-id hash"). Every
// field here except `control` is read-only, reflecting files `batch.py`/its wrapper own —
// the dashboard never writes queue or state files. `control` (WP-4,
// `plans/bakeoff-queue-control-plane.md`) is the one addition: an optional, independently
// null-honest section over the host control plane, added below.

/** Cell state batch.py can leave a cell in (`<stem>.state.json` `cells[cellId].state`). */
export const CellStateSchema = z.enum(["pending", "running", "completed", "failed", "skipped-propagated", "aborted-hang"]);
export type CellState = z.infer<typeof CellStateSchema>;

/** Queue-wide state (`<stem>.state.json`'s top-level `queue_state`). */
export const QueueStateSchema = z.enum(["running", "paused-resumable", "completed", "aborted-hang"]);
export type QueueState = z.infer<typeof QueueStateSchema>;

/** Per-state cell counts for one queue. Computed from the sibling state file's own cell
 * entries (independent of whether a given queue cell's alias matches one of them) —
 * every key is always present; every value is `null` together when there is no state
 * file at all (nothing is known yet), never a mix of `null` and counted. */
export const QueueCellCountsSchema = z.object({
  pending: z.number().int().nonnegative().nullable(),
  running: z.number().int().nonnegative().nullable(),
  completed: z.number().int().nonnegative().nullable(),
  failed: z.number().int().nonnegative().nullable(),
  "skipped-propagated": z.number().int().nonnegative().nullable(),
  "aborted-hang": z.number().int().nonnegative().nullable(),
});
export type QueueCellCounts = z.infer<typeof QueueCellCountsSchema>;

/** One queue cell, queue-file fields joined against its state-file match (by alias) when
 * one exists. `state`/`reason` are `null` when the queue has no state file yet, OR when
 * this cell's alias matches no state entry (a state file predating WP-B carries no alias
 * anywhere — the committed `wp5-validation` pair — so every one of its cells renders
 * unjoined this way too, never a fabricated match). */
export const QueueCellSchema = z.object({
  alias: z.string(),
  model: z.string().nullable(),
  c: z.number().int().nullable(),
  ctk: z.string().nullable(),
  ctv: z.string().nullable(),
  fa: z.string().nullable(),
  samplingArm: z.string().nullable(),
  trials: z.number().int().nullable(),
  state: CellStateSchema.nullable(),
  reason: z.string().nullable(),
});
export type QueueCell = z.infer<typeof QueueCellSchema>;

/** Per-queue ETA (`plans/bench-dashboard-polish.md` WP-2; model/context-aware fallback
 * chain added by `plans/bakeoff-timing-records-and-estimates.md` WP-2) — derived API-side,
 * never from anything the browser reads itself. `basis: null` (with
 * `medianCellMinutes`/`estimatedMinutesRemaining`/`cellsPerHour`/`sampleSize` all `null`
 * alongside it) means "no timing basis yet" — the null-honest state the UI renders as a
 * quiet note, never an error. `remainingCells` is computed straight from `cellCounts`
 * (`pending + running`) independently of whether any timing basis exists — it's a cell
 * count, not a derived time estimate, so it's populated whenever a sibling state file is,
 * even with zero timing data.
 *
 * `basis: "model-table"` (WP-2, timing-records plan): the estimate used a per-(model,
 * context) cost table built from this queue's own records (`apps/api/src/queue/eta.ts`)
 * for at least one remaining cell, instead of (or alongside) the plain consecutive-
 * timestamp global median above — supersedes `"current-run"`/`"prior-runs"` for that
 * queue's headline the moment any remaining cell resolves this way. `bucketSampleSize`
 * (total per-cell cost samples consumed across the whole estimate) and `scalingCaveat`
 * (true whenever the total is not purely exact-(model,context)-basis — some remaining cell
 * fell back to a same-model-scaled projection, the plain global median, or was dropped
 * entirely for lack of any basis) are both `.optional()` alongside `.nullable()`: they are
 * only ever present when `basis === "model-table"`; every other case omits them entirely
 * so the wire JSON for "no better basis exists" stays byte-identical to before this field
 * pair existed (honesty contract — never a fabricated `false`/`0` standing in for "not
 * evaluated"). */
export const QueueEtaSchema = z.object({
  remainingCells: z.number().int().nonnegative().nullable(),
  medianCellMinutes: z.number().nullable(),
  estimatedMinutesRemaining: z.number().nullable(),
  cellsPerHour: z.number().nullable(),
  basis: z.enum(["current-run", "prior-runs", "model-table"]).nullable(),
  sampleSize: z.number().int().nonnegative().nullable(),
  bucketSampleSize: z.number().int().nonnegative().nullable().optional(),
  scalingCaveat: z.boolean().nullable().optional(),
});
export type QueueEta = z.infer<typeof QueueEtaSchema>;

/** A successfully read+joined queue. `eta` is `.optional()` for the same reason `control`
 * below is: the API always sends the key (`routes/queue.ts` populates it on every push),
 * but this schema is also `.parse()`d against pre-WP-2 literals in shared/schema round-trip
 * tests that predate the field — `.optional()` keeps those parseable (never a breaking
 * change) exactly like `QueueListResponseSchema.control`'s own precedent. */
export const QueueEntrySchema = z.object({
  name: z.string(),
  queueState: QueueStateSchema.nullable(),
  deadline: z.string().nullable(),
  vramProfile: z.string().nullable(),
  cellCounts: QueueCellCountsSchema,
  cells: z.array(QueueCellSchema),
  eta: QueueEtaSchema.optional(),
});
export type QueueEntry = z.infer<typeof QueueEntrySchema>;

/** A queue or its sibling state file that failed to parse — the route reports this queue
 * entry with a reason instead of ever 500ing the whole route over one bad file. `eta` is
 * `null` here (not a null-filled `QueueEtaSchema` object): there is no queue/cell data at
 * all to derive even `remainingCells` from, unlike the "known cells, no timing basis yet"
 * case on `QueueEntrySchema` above. `.optional()` for the same pre-WP-2-literal backward
 * compatibility reason as `QueueEntrySchema.eta`. */
export const QueueErrorEntrySchema = z.object({
  name: z.string(),
  error: z.string(),
  eta: z.null().optional(),
});
export type QueueErrorEntry = z.infer<typeof QueueErrorEntrySchema>;

export const QueueListItemSchema = z.union([QueueEntrySchema, QueueErrorEntrySchema]);
export type QueueListItem = z.infer<typeof QueueListItemSchema>;

/** `GET /api/queue` response. Missing/empty `QUEUES_DIR` -> `{queues: []}` (200, never an
 * error — no queues configured is a valid, common state, not a failure).
 *
 * `control` (cycle-1 M4, WP-4): `QueueListResponseSchema` is a CLOSED zod object, so this
 * field must be declared here explicitly — omitting it silently strips `control` from
 * every parse, including on the web client. `.nullable().optional()`: `null` when the
 * control plane is unset/unreachable/unparseable (the API always sends the key once
 * `control.ts` support lands), `undefined` only for responses built before this field
 * existed (kept parseable, never a breaking change). */
export const QueueListResponseSchema = z.object({
  queues: z.array(QueueListItemSchema),
  generatedAt: z.string(),
  control: ControlSectionSchema.nullable().optional(),
});
export type QueueListResponse = z.infer<typeof QueueListResponseSchema>;
