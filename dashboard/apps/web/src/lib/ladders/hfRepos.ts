import type { ExperimentSummary } from "@bench-dashboard/shared";

/** Client-side derivation of the hf_repo picker list for the Ladders view (plan WP-2
 * bullet 2: "derive the list client-side from the experiments response lineage" -- there
 * is no dedicated "list distinct repos" API route). Sorted for a stable picker order;
 * null/empty `hfRepo` values are dropped -- a record with no resolvable lineage has no
 * ladder to join against. */
export function deriveHfRepos(experiments: ExperimentSummary[]): string[] {
  const repos = new Set<string>();
  for (const exp of experiments) {
    if (exp.lineage.hfRepo !== null && exp.lineage.hfRepo.length > 0) repos.add(exp.lineage.hfRepo);
  }
  return [...repos].sort((a, b) => a.localeCompare(b));
}
