import type { ServingAlias, ServingMeasuredEntry } from "@bench-dashboard/shared";

export interface ServingMatchRow {
  alias: ServingAlias;
  measured: ServingMeasuredEntry | null;
}

/** Joins `GET /api/serving`'s alias list to its best measured cell client-side (plan WP-2
 * bullet 5: "each joined client-side to its best measured cell (match alias against
 * experiment sweepCell.alias)"). The alias contract (root `CLAUDE.md`: "append-only --
 * never rename, never repurpose") makes this a literal string match, never fuzzy.
 * `measured` arrives from `GET /api/serving/measured` (the `serving-facts-v1` file's
 * `measured` list, `research-to-eval-harness` WP-4), which the research side writes in its
 * `/api/experiments` order -- newest-first -- so when more than one experiment shares a sweep
 * alias (a re-measured cell) `.find` naturally picks the most recent one: "best measured
 * cell" reads as "most recently measured" here. An experiment with no `sweepCell` is never
 * in the list -- it has no alias to match against. */
export function matchServingToMeasured(
  aliases: readonly ServingAlias[],
  measured: readonly ServingMeasuredEntry[],
): ServingMatchRow[] {
  return aliases.map((alias) => {
    const hit = measured.find((entry) => alias.aliases.includes(entry.alias)) ?? null;
    return { alias, measured: hit };
  });
}
