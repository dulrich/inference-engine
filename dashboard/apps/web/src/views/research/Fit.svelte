<script lang="ts">
  // Fit -- VRAM measurements (plan WP-2 bullet 4). `metrics` rows (vram_baseline/
  // vram_loaded/vram_after_teardown) from the detail route, rendered per experiment in GiB
  // with the deltas; empty state for experiments without VRAM metrics. NOTE (decision, see
  // report): the API carries no "predicted VRAM" field anywhere in the frozen v2 schema --
  // only these three measured snapshots -- so this view shows measured values and their
  // deltas, never a fabricated prediction.
  import type { ExperimentSummary } from "@bench-dashboard/shared";
  import { getExperimentDetail } from "../../api.ts";
  import { computeVramFitRow, type VramFitRow } from "../../lib/fit/vram.ts";
  import { formatGiB, formatSignedGiB } from "../../lib/format.ts";

  let {
    experiments,
    loading,
    error,
    refreshToken,
  }: {
    experiments: ExperimentSummary[];
    loading: boolean;
    error: string;
    refreshToken: number;
  } = $props();

  interface FitRow {
    experiment: ExperimentSummary;
    vram: VramFitRow;
  }

  let fitRows = $state<FitRow[]>([]);
  let excludedCount = $state(0);
  let fitLoading = $state(false);
  let fitError = $state("");

  async function loadFit(exps: ExperimentSummary[]) {
    if (exps.length === 0) {
      fitRows = [];
      excludedCount = 0;
      return;
    }
    fitLoading = true;
    fitError = "";
    try {
      const details = await Promise.all(exps.map((e) => getExperimentDetail(e.id)));
      const computed = details.map((d, i) => ({ experiment: exps[i] as ExperimentSummary, vram: computeVramFitRow(d.metrics) }));
      const withVram: FitRow[] = [];
      for (const c of computed) {
        if (c.vram !== null) withVram.push({ experiment: c.experiment, vram: c.vram });
      }
      fitRows = withVram;
      excludedCount = computed.length - fitRows.length;
    } catch (e) {
      fitError = e instanceof Error ? e.message : "Unable to load VRAM metrics";
    } finally {
      fitLoading = false;
    }
  }

  // ONE effect tracking both `experiments` and `refreshToken` -- see Runs.svelte's own
  // comment on why this isn't split into two effects.
  $effect(() => {
    // eslint-disable-next-line no-unused-expressions -- bare read registers the $effect dependency
    refreshToken;
    void loadFit(experiments);
  });
</script>

<p class="eyebrow">VRAM measurements</p>
<h1>Fit</h1>
<p class="run-lede">
  Measured VRAM snapshots per experiment -- baseline (before load), loaded, and after teardown -- with the load and
  teardown-gap deltas.
</p>

{#if loading || fitLoading}
  <p>Loading…</p>
{:else if error}
  <p class="error-text">{error}</p>
{:else if fitError}
  <p class="error-text">{fitError}</p>
{:else if experiments.length === 0}
  <p class="run-empty">No experiments have been ingested yet.</p>
{:else if fitRows.length === 0}
  <p class="run-empty">No ingested experiment carries VRAM snapshots yet.</p>
{:else}
  <div class="run-table-wrap">
    <table class="run-table">
      <thead>
        <tr>
          <th>Run</th>
          <th class="run-table-num">Baseline</th>
          <th class="run-table-num">Loaded</th>
          <th class="run-table-num">After teardown</th>
          <th class="run-table-num">Load delta</th>
          <th class="run-table-num">Teardown gap</th>
        </tr>
      </thead>
      <tbody>
        {#each fitRows as row (row.experiment.id)}
          <tr>
            <td>{row.experiment.sweepCell?.alias ?? row.experiment.id}</td>
            <td class="run-table-num">{formatGiB(row.vram.baselineBytes)}</td>
            <td class="run-table-num">{formatGiB(row.vram.loadedBytes)}</td>
            <td class="run-table-num">{formatGiB(row.vram.afterTeardownBytes)}</td>
            <td class="run-table-num">{formatSignedGiB(row.vram.loadDeltaBytes)}</td>
            <td class="run-table-num">{formatSignedGiB(row.vram.teardownGapBytes)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
  {#if excludedCount > 0}
    <p class="run-note">{excludedCount} experiment(s) have no VRAM metrics yet and are not shown above.</p>
  {/if}
{/if}
