import type { ServingTimingRow } from "@bench-dashboard/shared";
import { formatTokS } from "../format.ts";

/** Joins a served alias list (`ServingAlias.aliases`) against `GET /api/serving/timing`'s
 * per-alias p50 tok/s aggregate (WP-3, `plans/bakeoff-timing-records-and-estimates.md`) --
 * a literal string match, same alias-contract discipline `matchServingToMeasured` already
 * uses ("append-only -- never rename, never repurpose", root `CLAUDE.md`). A served model
 * row can carry MULTIPLE aliases; it matches this aggregate if ANY one of them does. An
 * alias with no `timing_round` samples (the `bge-*` embed/rerank aliases are the known case
 * today) simply has no row in `timingRows` -- returns `null`, never a guessed match. */
export function matchServingTiming(
  aliases: readonly string[],
  timingRows: readonly ServingTimingRow[],
): ServingTimingRow | null {
  return timingRows.find((row) => aliases.includes(row.alias)) ?? null;
}

/** Renders the Serving tab's "tok/s (p50)" cell: `formatTokS`'s value plus a small
 * `n=<sampleCount>` suffix when a match exists. The literal "—" (never "n=0" or a
 * fabricated zero) when `matchServingTiming` found nothing -- the same null-honest posture
 * every other cell in this view already carries for "no measurement yet". */
export function formatServingTimingCell(row: ServingTimingRow | null): string {
  if (row === null) return "—";
  return `${formatTokS(row.p50DecodeTokS)} (n=${row.sampleCount})`;
}
