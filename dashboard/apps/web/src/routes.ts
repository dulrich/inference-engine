// Hand-rolled path routing for the app's seven views (Runs/Ladders/Long-context/Fit/
// Serving/Queue/Router) plus Runs' drill-down -- following `fleet-tools/model_scoreboard`'s
// own `apps/web/src/routes.ts` precedent (pattern router, no dependency; the API's SPA
// fallback, `apps/api/src/app.ts spaResponse`, makes every path here refresh-safe; History
// API driven directly from App.svelte). Queue (WP-C, plan
// `bench-dashboard-run-context-surfacing.md` WP-C) is a read-only view over `GET
// /api/queue` -- no drill-down path of its own.
//
// Serving owns "/" (WP-2, `plans/capability-surfacing-and-local-agent-endpoints.md`: "Serving
// becomes the default landing route of the SPA") -- Runs moved to its own bare `/runs` path,
// distinct from the existing `/runs/:id` drill-down pattern below (`RUN_DETAIL_PATH` requires
// a non-empty id segment, so it never matches the bare `/runs`).
//
// Router (WP-4, `plans/router-observability-and-tool-search-conformance.md`) is the seventh
// view -- read-only over `GET /api/router/{telemetry,sidecar}`, no drill-down path of
// its own, same `{refreshToken}`-only prop shape as Queue.

export type Tab = "runs" | "ladders" | "longcontext" | "fit" | "serving" | "queue" | "router";

const TAB_PATHS: Record<Tab, string> = {
  runs: "/runs",
  ladders: "/ladders",
  longcontext: "/longcontext",
  fit: "/fit",
  serving: "/",
  queue: "/queue",
  router: "/router",
};

export function tabToPath(tab: Tab): string {
  return TAB_PATHS[tab];
}

/** `/runs/:id` -- an experiment's drill-down page. */
export function runDetailPath(id: string): string {
  return `/runs/${encodeURIComponent(id)}`;
}

export interface ParsedRoute {
  tab: Tab;
  /** Present only when `tab === "runs"` and the path named a specific experiment id. */
  runId?: string;
  // False for any path that isn't one of the recognized routes below -- the caller
  // normalizes the URL to "/" in that case.
  matched: boolean;
}

// An unrecognized path normalizes to "/" (below, `applyLocation`'s `history.replaceState`),
// so its fallback tab must match whatever tab "/" itself resolves to -- `serving` since
// WP-2, matching `TAB_PATHS.serving` above.
const UNMATCHED: ParsedRoute = { tab: "serving", matched: false };

const RUN_DETAIL_PATH = /^\/runs\/([^/]+)$/;

export function parsePath(pathname: string): ParsedRoute {
  const runMatch = pathname.match(RUN_DETAIL_PATH);
  if (runMatch) {
    const runId = decodeURIComponent(runMatch[1] as string);
    return runId.length > 0 ? { tab: "runs", runId, matched: true } : UNMATCHED;
  }

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
