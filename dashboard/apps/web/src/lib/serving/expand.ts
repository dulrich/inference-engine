// Serving view's per-row expand/collapse (WP-1, `plans/bench-dashboard-polish.md`) --
// plain, DOM-free helpers so the toggle state and the full-config flag/value rendering are
// unit-testable, not buried in `Serving.svelte` (brief's "testable logic lives in plain .ts
// helpers" pin). Signalworks CC-04: the toggle itself is always a text state word ("show
// config" / "hide config"), never icon-only or color-only -- `expandToggleLabel` is the one
// place that word pair lives, so the component and its tests share a single source of truth
// for it.

import type { ServingFlagEntry } from "@bench-dashboard/shared";

// ---- Per-row expanded-state set -------------------------------------------------------
// Keyed by `ServingAlias.modelKey` (unique per row, same key `Serving.svelte`'s `{#each}`
// already uses). Returns a NEW Set rather than mutating in place -- keeps this a pure
// function Svelte's `$state` reassignment can drive directly, and keeps it trivially
// testable without a component harness.

export function toggleExpandedRow(expanded: ReadonlySet<string>, key: string): Set<string> {
  const next = new Set(expanded);
  if (next.has(key)) {
    next.delete(key);
  } else {
    next.add(key);
  }
  return next;
}

// ---- CC-04 state word ------------------------------------------------------------------

export function expandToggleLabel(isExpanded: boolean): string {
  return isExpanded ? "hide config" : "show config";
}

// ---- Flag/value table rendering ---------------------------------------------------------
// A boolean-style flag (`ServingFlagEntry.value === null`, e.g. `--jinja`) renders as the
// same null-safe em-dash every other view uses for "nothing to show" (`format.ts`'s
// `formatCount`/`formatGiB`, `Serving.svelte`'s own `kvCacheTypeK ?? "—"`) rather than a
// bespoke "true"/"(flag)" label -- `null` here is a known, valid state (the flag IS present
// in the served command), not missing data, but the display convention this app already
// uses for "no value to print" still reads correctly for it.

export function formatFlagValue(value: ServingFlagEntry["value"]): string {
  return value === null ? "—" : value;
}

/** Reuses the existing `serving-table-muted` class (no new CSS token, CC-15/CC-01) to visually
 * de-emphasize a boolean-style flag's value cell the same way the rest of the app already
 * de-emphasizes "no measurement yet"/"—" cells. */
export function flagValueClass(value: ServingFlagEntry["value"]): string {
  return value === null ? "serving-table-muted" : "";
}
