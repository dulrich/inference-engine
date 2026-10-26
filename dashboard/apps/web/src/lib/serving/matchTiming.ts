import type { ServingTimingRow } from "@bench-dashboard/shared";

/** Joins a served model's `modelKey` to its `timing` row from `GET /api/serving/facts`
 * (exact match, alias contract); no row is `null`, never a guessed match. */
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
