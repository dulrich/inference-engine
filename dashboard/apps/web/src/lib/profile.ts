// `vram_profile` chip -- shared by Runs (list row + detail) and Queue (WP-C, plan
// `bench-dashboard-run-context-surfacing.md` WP-C). WP-B stamps the effective profile into
// record `config.json`; queue files already carry their own top-level `vram_profile`.
// `desktop-shared` / `headless` are the only values `batch.py` writes today -- any other
// non-null string is a future profile this dashboard hasn't seen yet, shown verbatim
// (never hidden) rather than falling back to muted; `null` is a pre-existing record/queue
// file this plan doesn't backfill (Assumptions: "display unknown/unjoined").

export function vramProfileLabel(profile: string | null): string {
  return profile ?? "unknown";
}

export function vramProfilePillClass(profile: string | null): string {
  if (profile === null) return "pill-muted";
  if (profile === "desktop-shared") return "pill-warn";
  if (profile === "headless") return "pill-ok";
  return "pill-info";
}
