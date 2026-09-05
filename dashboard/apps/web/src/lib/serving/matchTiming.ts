import type { ServingTimingRow } from "@bench-dashboard/shared";

/** Joins a served model's `modelKey` against `GET /api/serving/timing`'s per-served-model
 * p50/p90 aggregate (WP-1, `plans/served-recipe-p50-prefill-and-decode.md` -- rekeys the
 * WP-3 original from a raw `sweep_alias` string match to the resolver's own `modelKey`,
 * since a timing row is now already resolved to exactly one served model server-side). An
 * exact match, same alias-contract discipline `matchServingToMeasured` already uses
 * ("append-only -- never rename, never repurpose", root `CLAUDE.md`) -- a served model with
 * no recipe-matched `timing_round` rounds (the `bge-*` embed/rerank aliases are the known
 * case today) simply has no row in `timingRows` -- returns `null`, never a guessed match. */
export function matchServingTiming(modelKey: string, timingRows: readonly ServingTimingRow[]): ServingTimingRow | null {
  return timingRows.find((row) => row.modelKey === modelKey) ?? null;
}

/** `(arm, n=…)` provenance suffix shared by both formatters below -- an unlabelled arm
 * (`samplingArm: null`) renders as the literal `unlabelled`, never a blank or "null". */
function armSuffix(row: ServingTimingRow): string {
  return `(${row.samplingArm ?? "unlabelled"}, n=${row.sampleCount})`;
}

function formatPair(p50: number | null, p90: number | null): string | null {
  if (p50 === null || p90 === null) return null;
  return `${p50.toFixed(1)} / ${p90.toFixed(1)} tok/s`;
}

/** Renders the Serving tab's "Prefill tok/s (p50/p90)" cell. The literal "—" (never a
 * fabricated pair) both when there's no matched row at all AND when the matched row's own
 * prefill percentiles are null (prefill samples may be fewer than decode samples within
 * the same admissible round set -- plan Decisions locked -- so a real match can still carry
 * no prefill numbers). */
export function formatServingPrefillCell(row: ServingTimingRow | null): string {
  if (row === null) return "—";
  const pair = formatPair(row.p50PrefillTokS, row.p90PrefillTokS);
  return pair === null ? "—" : `${pair} ${armSuffix(row)}`;
}

/** Renders the Serving tab's "Decode tok/s (p50/p90)" cell -- same null-honest posture as
 * `formatServingPrefillCell` above. */
export function formatServingDecodeCell(row: ServingTimingRow | null): string {
  if (row === null) return "—";
  const pair = formatPair(row.p50DecodeTokS, row.p90DecodeTokS);
  return pair === null ? "—" : `${pair} ${armSuffix(row)}`;
}
