<script lang="ts">
  // Serving -- live-served aliases joined to their best measured cell (plan WP-2 bullet 5).
  // `GET /api/serving` is a server-side reader of `llama/config.yaml`; the SPA never reads
  // any host file directly (Decisions locked "Read-only over serving config"). The join to
  // `experiments` is client-side (`lib/serving/matchMeasured.ts`) -- most aliases have no
  // measurement yet, which is an expected, designed state, not an error.
  import type { ExperimentSummary } from "@bench-dashboard/shared";
  import { getServing } from "../api.ts";
  import { matchServingToMeasured } from "../lib/serving/matchMeasured.ts";
  import { guardedNavClick, runDetailPath } from "../routes.ts";
  import { formatCount } from "../lib/format.ts";

  let {
    experiments,
    loading,
    error,
    refreshToken,
    onopenrun,
  }: {
    experiments: ExperimentSummary[];
    loading: boolean;
    error: string;
    refreshToken: number;
    onopenrun: (id: string) => void;
  } = $props();

  let configPath = $state("");
  let rows = $state<ReturnType<typeof matchServingToMeasured>>([]);
  let servingLoading = $state(false);
  let servingError = $state("");

  async function loadServing(exps: ExperimentSummary[]) {
    servingLoading = true;
    servingError = "";
    try {
      const response = await getServing();
      configPath = response.configPath;
      rows = matchServingToMeasured(response.models, exps);
    } catch (e) {
      servingError = e instanceof Error ? e.message : "Unable to load served aliases";
    } finally {
      servingLoading = false;
    }
  }

  // ONE effect tracking both `experiments` and `refreshToken` -- see Runs.svelte's own
  // comment on why this isn't split into two effects.
  $effect(() => {
    refreshToken;
    void loadServing(experiments);
  });
</script>

<p class="eyebrow">Live vs. measured</p>
<h1>Serving</h1>
<p class="run-lede">
  Aliases currently served by the llama router, joined to their most recent measured cell where one exists.
  {#if configPath}<code>{configPath}</code>{/if}
</p>

{#if loading || servingLoading}
  <p>Loading…</p>
{:else if error}
  <p class="error-text">{error}</p>
{:else if servingError}
  <p class="error-text">{servingError}</p>
{:else if rows.length === 0}
  <p class="run-empty">
    No aliased models resolved from the served config -- either <code>LLAMA_CONFIG_PATH</code> is unset, or the
    config has no models carrying an <code>aliases:</code> list.
  </p>
{:else}
  <div class="run-table-wrap">
    <table class="run-table">
      <thead>
        <tr>
          <th>Aliases</th>
          <th>Name</th>
          <th class="run-table-num">Context</th>
          <th>KV K</th>
          <th>KV V</th>
          <th>FA</th>
          <th>Measured cell</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as row (row.alias.modelKey)}
          <tr>
            <td class="serving-aliases">{row.alias.aliases.join(", ")}</td>
            <td>{row.alias.name ?? row.alias.modelKey}</td>
            <td class="run-table-num">{formatCount(row.alias.flags.contextLength)}</td>
            <td>{row.alias.flags.kvCacheTypeK ?? "—"}</td>
            <td>{row.alias.flags.kvCacheTypeV ?? "—"}</td>
            <td>{row.alias.flags.flashAttention ?? "—"}</td>
            <td>
              {#if row.measured}
                <a
                  class="serving-measured-link"
                  href={runDetailPath(row.measured.id)}
                  onclick={(event) => guardedNavClick(event, () => onopenrun((row.measured as ExperimentSummary).id))}
                >
                  {row.measured.sweepCell?.alias ?? row.measured.id}
                </a>
              {:else}
                <span class="run-table-muted">no measurement yet</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}
