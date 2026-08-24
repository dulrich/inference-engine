// Output-length line (Runs drill-down, WP-C: `responseCaps` + completion-token stats +
// finish-reason counts). `responseCaps` itself (`ExperimentDetailSchema.responseCaps`) is
// rendered directly by the view -- these two summarize the per-trial `completionTokens`/
// `finishReason` columns `TrialRowSchema` already carries, the same "pure function over
// wire rows" shape `lib/format.ts`/`lib/runs/verdict.ts` use.

import type { TrialRow } from "@bench-dashboard/shared";

export interface CompletionTokenStats {
  count: number;
  min: number;
  max: number;
  mean: number;
}

/** `null` when no trial on this run carries a non-null `completionTokens` (a timing-only
 * record, or one predating this field) -- never a stats object built over zero samples. */
export function summarizeCompletionTokens(trials: Pick<TrialRow, "completionTokens">[]): CompletionTokenStats | null {
  const values = trials.map((t) => t.completionTokens).filter((v): v is number => v !== null);
  if (values.length === 0) return null;
  const sum = values.reduce((a, b) => a + b, 0);
  return {
    count: values.length,
    min: Math.min(...values),
    max: Math.max(...values),
    mean: sum / values.length,
  };
}

export interface ResponseCapRow {
  task: string;
  cap: number;
}

/** Flattens WP-B's `response_caps` shape — `{default: <n>, by_task: {<task>: <n>}}` — into
 * renderable rows: one `default` row plus one per `by_task` entry. The wire type is loose
 * (`z.record(z.string(), z.unknown())`), so anything non-numeric is skipped rather than
 * stringified (an `Object.entries` render of the raw object would print `by_task` as
 * `[object Object]` — the exact defect this function exists to prevent). Unrecognized
 * top-level numeric keys render as their own rows (a future cap variant stays visible). */
export function flattenResponseCaps(caps: Record<string, unknown> | null): ResponseCapRow[] {
  if (caps === null) return [];
  const rows: ResponseCapRow[] = [];
  for (const [key, value] of Object.entries(caps)) {
    if (typeof value === "number" && Number.isFinite(value)) {
      rows.push({ task: key, cap: value });
    } else if (key === "by_task" && typeof value === "object" && value !== null && !Array.isArray(value)) {
      for (const [task, cap] of Object.entries(value)) {
        if (typeof cap === "number" && Number.isFinite(cap)) rows.push({ task, cap });
      }
    }
  }
  return rows;
}

export interface FinishReasonCount {
  reason: string;
  count: number;
}

/** Finish-reason counts across a run's trials, sorted by descending count then reason name
 * for a stable render order. Trials with a `null` `finishReason` contribute nothing --
 * there is no "unknown" bucket to show alongside real reasons. */
export function summarizeFinishReasons(trials: Pick<TrialRow, "finishReason">[]): FinishReasonCount[] {
  const counts = new Map<string, number>();
  for (const t of trials) {
    if (t.finishReason === null) continue;
    counts.set(t.finishReason, (counts.get(t.finishReason) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason));
}
