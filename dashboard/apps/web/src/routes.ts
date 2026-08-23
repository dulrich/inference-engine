// Hand-rolled path routing for the app's five views (Runs/Ladders/Long-context/Fit/
// Serving) plus Runs' drill-down -- following `fleet-tools/model_scoreboard`'s own
// `apps/web/src/routes.ts` precedent (pattern router, no dependency; the API's SPA
// fallback, `apps/api/src/app.ts spaResponse`, makes every path here refresh-safe; History
// API driven directly from App.svelte).

export type Tab = "runs" | "ladders" | "longcontext" | "fit" | "serving";

const TAB_PATHS: Record<Tab, string> = {
  runs: "/",
  ladders: "/ladders",
  longcontext: "/longcontext",
  fit: "/fit",
  serving: "/serving",
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

const UNMATCHED: ParsedRoute = { tab: "runs", matched: false };

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
