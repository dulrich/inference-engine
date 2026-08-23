import type { ExperimentSummary, ServingAlias } from "@bench-dashboard/shared";

export interface ServingMatchRow {
  alias: ServingAlias;
  measured: ExperimentSummary | null;
}

/** Joins `GET /api/serving`'s alias list to its best measured cell client-side (plan WP-2
 * bullet 5: "each joined client-side to its best measured cell (match alias against
 * experiment sweepCell.alias)"). The alias contract (root `CLAUDE.md`: "append-only --
 * never rename, never repurpose") makes this a literal string match, never fuzzy.
 * `experiments` arrives from `GET /api/experiments` already ordered newest-first
 * (`db/experiments.ts listExperimentSummaries`, `ORDER BY timestamp DESC`), so when more
 * than one experiment shares a sweep alias (a re-measured cell) `.find` naturally picks the
 * most recent one -- "best measured cell" reads as "most recently measured" here, there
 * being no other ranking signal (tok/s, correctness) that would make sense to rank by
 * instead without more product direction). An experiment with no `sweepCell` (a non-sweep
 * `full`-CLI record) never matches -- it has no alias to match against. */
export function matchServingToMeasured(
  aliases: readonly ServingAlias[],
  experiments: readonly ExperimentSummary[],
): ServingMatchRow[] {
  return aliases.map((alias) => {
    const measured =
      experiments.find((exp) => exp.sweepCell !== null && alias.aliases.includes(exp.sweepCell.alias)) ?? null;
    return { alias, measured };
  });
}
