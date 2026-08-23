import type { MetricRow } from "@bench-dashboard/shared";

export interface VramFitRow {
  baselineBytes: number | null;
  loadedBytes: number | null;
  afterTeardownBytes: number | null;
  /** loaded - baseline: what the model actually cost to load. */
  loadDeltaBytes: number | null;
  /** afterTeardown - baseline: residual VRAM the teardown did not reclaim (0 for a clean
   * teardown; the Fit view's "measured vs baseline" number, plan WP-2 bullet 4's "deltas"). */
  teardownGapBytes: number | null;
}

function findBytes(metrics: readonly MetricRow[], metricType: string): number | null {
  return metrics.find((m) => m.metricType === metricType)?.bytes ?? null;
}

/** VRAM snapshot row for the Fit view (plan WP-2 bullet 4): pulls the three `vram_*`
 * `MetricRow`s (`metricType` `vram_baseline`/`vram_loaded`/`vram_after_teardown`) a detail
 * response may carry, and computes the deltas the view renders. Returns `null` when NONE
 * of the three snapshots are present -- the view's own empty-state trigger ("experiments
 * without VRAM metrics"); a PARTIAL snapshot (e.g. baseline present, loaded missing) still
 * returns a row with the missing field(s) null, so the table shows what it has rather than
 * hiding a partial measurement entirely. */
export function computeVramFitRow(metrics: readonly MetricRow[]): VramFitRow | null {
  const baselineBytes = findBytes(metrics, "vram_baseline");
  const loadedBytes = findBytes(metrics, "vram_loaded");
  const afterTeardownBytes = findBytes(metrics, "vram_after_teardown");
  if (baselineBytes === null && loadedBytes === null && afterTeardownBytes === null) return null;
  return {
    baselineBytes,
    loadedBytes,
    afterTeardownBytes,
    loadDeltaBytes: baselineBytes !== null && loadedBytes !== null ? loadedBytes - baselineBytes : null,
    teardownGapBytes: baselineBytes !== null && afterTeardownBytes !== null ? afterTeardownBytes - baselineBytes : null,
  };
}
