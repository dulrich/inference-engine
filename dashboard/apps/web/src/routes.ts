// Hand-rolled path routing for the serving dashboard's two views (Serving/Router) --
// following `fleet-tools/model_scoreboard`'s own `apps/web/src/routes.ts` precedent
// (pattern router, no dependency; the server's SPA fallback makes every path here
// refresh-safe; History API driven directly from App.svelte). Serving owns "/" (the default
// landing route). The research views (Runs/Ladders/Long-context/Fit/Queue) moved to the
// research dashboard (eval-harness `inference_research`, :4816; `research-to-eval-harness`
// WP-4); their old paths are unmatched here and normalize to "/".

export type Tab = "serving" | "router";

/** The nav areas: Serving and Router, one tab each. */
export type NavArea = "serving" | "router";

export const NAV_GROUPS: { area: NavArea; label: string; tabs: { tab: Tab; label: string }[] }[] = [
  { area: "serving", label: "Serving", tabs: [{ tab: "serving", label: "Serving" }] },
  { area: "router", label: "Router", tabs: [{ tab: "router", label: "Router" }] },
];

/** The area a tab belongs to (drives the active-group highlight). */
export function tabArea(tab: Tab): NavArea {
  return (NAV_GROUPS.find((g) => g.tabs.some((t) => t.tab === tab)) as (typeof NAV_GROUPS)[number]).area;
}

const TAB_PATHS: Record<Tab, string> = {
  serving: "/",
  router: "/router",
};

export function tabToPath(tab: Tab): string {
  return TAB_PATHS[tab];
}

export interface ParsedRoute {
  tab: Tab;
  // False for any path that isn't one of the recognized routes below -- the caller
  // normalizes the URL to "/" in that case.
  matched: boolean;
}

// An unrecognized path normalizes to "/" (below, `applyLocation`'s `history.replaceState`),
// so its fallback tab must match whatever tab "/" itself resolves to -- `serving`, matching
// `TAB_PATHS.serving` above.
const UNMATCHED: ParsedRoute = { tab: "serving", matched: false };

export function parsePath(pathname: string): ParsedRoute {
  const entry = (Object.entries(TAB_PATHS) as [Tab, string][]).find(([, path]) => path === pathname);
  if (entry) return { tab: entry[0], matched: true };

  return UNMATCHED;
}

/** Standard modified-click guard (`model_scoreboard App.svelte handleNavClick` /
 * `Feed.svelte handleLinkClick` precedent): lets ctrl/cmd/shift/alt clicks and non-primary
 * buttons (e.g. middle-click) fall through to the real `<a href>` so "open in new tab"
 * keeps working; only a plain left click is intercepted for SPA navigation. */
export function guardedNavClick(event: MouseEvent, action: () => void): void {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  event.preventDefault();
  action();
}
