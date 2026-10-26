import type { ServingAlias, ServingMeasuredEntry } from "@bench-dashboard/shared";

export interface ServingMatchRow {
  alias: ServingAlias;
  measured: ServingMeasuredEntry | null;
}

/** Joins `GET /api/serving`'s aliases to `GET /api/serving/facts`'s `measured` list by literal
 * alias match (alias contract: append-only, never fuzzy). The list is newest-first, so `.find`
 * picks the most recently measured cell. */
export function matchServingToMeasured(
  aliases: readonly ServingAlias[],
  measured: readonly ServingMeasuredEntry[],
): ServingMatchRow[] {
  return aliases.map((alias) => {
    const hit = measured.find((entry) => alias.aliases.includes(entry.alias)) ?? null;
    return { alias, measured: hit };
  });
}
