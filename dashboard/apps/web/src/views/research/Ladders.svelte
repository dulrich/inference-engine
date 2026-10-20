<script lang="ts">
  // Ladders -- per-model quant/KV/context table (plan WP-2 bullet 2). The hf_repo picker
  // list is derived client-side from `experiments` (no dedicated API route); rows come from
  // `GET /api/ladders/<hfRepo>`, re-fetched whenever the picked repo or refreshToken changes.
  import type { ExperimentSummary, LadderRow } from "@bench-dashboard/shared";
  import { getLadders } from "../../api.ts";
  import { formatCount, formatMs, formatPct, formatTokS } from "../../lib/format.ts";
  import { deriveHfRepos } from "../../lib/ladders/hfRepos.ts";

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

  const hfRepos = $derived(deriveHfRepos(experiments));
  let selectedRepo = $state<string | null>(null);
  let rows = $state<LadderRow[]>([]);
  let rowsLoading = $state(false);
  let rowsError = $state("");

  // Default to the first repo once the list arrives; never override a user's own pick.
  $effect(() => {
    if (selectedRepo === null && hfRepos.length > 0) selectedRepo = hfRepos[0] as string;
  });

  async function loadRows(repo: string) {
    rowsLoading = true;
    rowsError = "";
    try {
      rows = await getLadders(repo);
    } catch (e) {
      rowsError = e instanceof Error ? e.message : `Unable to load ladder rows for ${repo}`;
    } finally {
      rowsLoading = false;
    }
  }

  // ONE effect tracking both `selectedRepo` and `refreshToken` -- see Runs.svelte's own
  // comment on why this isn't split into two effects (a second effect reading
  // `selectedRepo` inside its body would re-track it too and double-fetch).
  $effect(() => {
    // eslint-disable-next-line no-unused-expressions -- bare read registers the $effect dependency
    refreshToken;
    if (selectedRepo !== null) void loadRows(selectedRepo);
  });

  function handleRepoChange(event: Event) {
    selectedRepo = (event.currentTarget as HTMLSelectElement).value;
  }
</script>

<p class="eyebrow">Per-model config sweep</p>
<h1>Ladders</h1>
<p class="run-lede">
  Quant filename &times; <code>-c</code> &times; <code>-ctk</code>/<code>-ctv</code> &times; <code>-fa</code>, one row
  per measured cell for the picked model.
</p>

{#if loading}
  <p>Loading…</p>
{:else if error}
  <p class="error-text">{error}</p>
{:else if hfRepos.length === 0}
  <p class="run-empty">
    No experiment carries a resolvable <code>hf_repo</code> yet -- ladders join on lineage, so there is nothing to
    pick a model from.
  </p>
{:else}
  <div class="field ladders-picker">
    <label for="ladders-repo">Model (hf_repo)</label>
    <select id="ladders-repo" value={selectedRepo} onchange={handleRepoChange}>
      {#each hfRepos as repo (repo)}
        <option value={repo}>{repo}</option>
      {/each}
    </select>
  </div>

  {#if rowsLoading}
    <p>Loading ladder…</p>
  {:else if rowsError}
    <p class="error-text">{rowsError}</p>
  {:else if rows.length === 0}
    <p class="run-empty">No measured cells for this model yet.</p>
  {:else}
    <div class="run-table-wrap">
      <table class="run-table">
        <thead>
          <tr>
            <th>Filename</th>
            <th>Alias</th>
            <th class="run-table-num">Context</th>
            <th>KV K</th>
            <th>KV V</th>
            <th>FA</th>
            <th class="run-table-num">Median tok/s</th>
            <th class="run-table-num">Median TTFT</th>
            <th class="run-table-num">Correctness</th>
            <th class="run-table-num">Trials</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as row (row.experimentId)}
            <tr>
              <td>{row.filename ?? "—"}</td>
              <td>{row.alias ?? "—"}</td>
              <td class="run-table-num">{formatCount(row.contextLength)}</td>
              <td>{row.kvCacheTypeK ?? "—"}</td>
              <td>{row.kvCacheTypeV ?? "—"}</td>
              <td>{row.flashAttention ?? "—"}</td>
              <td class="run-table-num">{formatTokS(row.medianDecodeTokS)}</td>
              <td class="run-table-num">{formatMs(row.medianFirstGenerationTokenMs)}</td>
              <td class="run-table-num">{formatPct(row.correctnessRate)}</td>
              <td class="run-table-num">{formatCount(row.trialCount)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
{/if}
