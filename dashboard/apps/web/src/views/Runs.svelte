<script lang="ts">
  // Runs -- the experiment browser (plan WP-2 bullet 1). List from `GET /api/experiments`
  // (already fetched by App.svelte, passed down); drill-down to `GET
  // /api/experiments/:id`'s per-trial rows, fetched here on `runId` change. CC-14: `loading`
  // flips once per fetch (list load is owned by App.svelte; detail load is this
  // component's own, keyed on `runId` so switching runs re-triggers it exactly once per
  // switch); every list loop below is keyed on its row id.
  import type { ExperimentSummary } from "@bench-dashboard/shared";
  import { getExperimentDetail, type ExperimentFullDetail } from "../api.ts";
  import { formatCount, formatGiB, formatMs, formatPct, formatShortHash, formatTimestamp, formatTokS } from "../lib/format.ts";
  import { extractClassifierFlags, formatClassifierFlags, verdictLabel, verdictPillClass } from "../lib/runs/verdict.ts";
  import { guardedNavClick, runDetailPath, tabToPath } from "../routes.ts";

  let {
    experiments,
    loading,
    error,
    runId,
    refreshToken,
    onopenrun,
    onback,
  }: {
    experiments: ExperimentSummary[];
    loading: boolean;
    error: string;
    runId?: string;
    refreshToken: number;
    onopenrun: (id: string) => void;
    onback: () => void;
  } = $props();

  let detail = $state<ExperimentFullDetail | null>(null);
  let detailLoading = $state(false);
  let detailError = $state("");

  async function loadDetail(id: string) {
    detailLoading = true;
    detailError = "";
    detail = null;
    try {
      detail = await getExperimentDetail(id);
    } catch (e) {
      detailError = e instanceof Error ? e.message : `Unable to load experiment ${id}`;
    } finally {
      detailLoading = false;
    }
  }

  // ONE effect tracking both `runId` (switching runs) and `refreshToken` (the app-wide poll
  // tick / manual refresh, same shape model_scoreboard's Analysis/Feed views use) -- reading
  // both inside a single effect body, rather than splitting them across two effects, avoids
  // a double fetch on every `runId` change (a second effect that also reads `runId` inside
  // its body would re-track it too and fire alongside this one).
  $effect(() => {
    refreshToken;
    if (runId !== undefined) {
      void loadDetail(runId);
    } else {
      detail = null;
      detailError = "";
    }
  });

  const wilsonSummary = $derived(detail?.wilsonSummary ?? null);
  const vramMetrics = $derived((detail?.metrics ?? []).filter((m) => m.metricType.startsWith("vram_")));
  const timingMetrics = $derived((detail?.metrics ?? []).filter((m) => m.metricType === "timing_round"));
</script>

{#if runId !== undefined}
  <!-- ---- Drill-down: one experiment's trials/metrics -------------------------------- -->
  <a class="btn-secondary back-link" href={tabToPath("runs")} onclick={(event) => guardedNavClick(event, onback)}>
    &larr; All runs
  </a>

  {#if detailLoading}
    <p>Loading experiment…</p>
  {:else if detailError}
    <p class="error-text">{detailError}</p>
  {:else if detail}
    <p class="eyebrow">Run detail</p>
    <h1>{detail.sweepCell?.alias ?? detail.id}</h1>
    {#if detail.description}<p class="run-description">{detail.description}</p>{/if}

    <section class="run-meta-grid">
      <div class="field">
        <span>Experiment id</span>
        <code>{detail.id}</code>
      </div>
      <div class="field">
        <span>Timestamp</span>
        <span>{formatTimestamp(detail.timestamp)}</span>
      </div>
      <div class="field">
        <span>HF repo</span>
        <span>{detail.lineage.hfRepo ?? "—"}</span>
      </div>
      <div class="field">
        <span>Filename</span>
        <span>{detail.lineage.filename ?? "—"}</span>
      </div>
      <div class="field">
        <span>Revision</span>
        <span>{formatShortHash(detail.lineage.hfRevision)}</span>
      </div>
      <div class="field">
        <span>sha256</span>
        <span>{formatShortHash(detail.lineage.sha256)}</span>
      </div>
      <div class="field">
        <span>llama.cpp build</span>
        <span>{detail.lineage.llamaCppBuild ?? "—"}</span>
      </div>
      {#if detail.sweepCell}
        <div class="field">
          <span>Sweep cell</span>
          <code>{detail.sweepCell.cellId}</code>
        </div>
      {/if}
    </section>

    {#if wilsonSummary}
      <h2>Wilson summary</h2>
      <p class="run-wilson">
        {formatCount(wilsonSummary.k)}/{formatCount(wilsonSummary.n)} failures &middot; rate {formatPct(wilsonSummary.rate)}
        &middot; {wilsonSummary.sided === "one" ? "one-sided" : "two-sided"} {formatPct(wilsonSummary.confidence)} interval
        [{formatPct(wilsonSummary.lower)}, {formatPct(wilsonSummary.upper)}]
      </p>
    {/if}

    {#if vramMetrics.length > 0}
      <h2>VRAM snapshots</h2>
      <div class="run-table-wrap">
        <table class="run-table">
          <thead>
            <tr>
              <th>Snapshot</th>
              <th class="run-table-num">Bytes</th>
            </tr>
          </thead>
          <tbody>
            {#each vramMetrics as m (m.id)}
              <tr>
                <td>{m.metricType}</td>
                <td class="run-table-num">{formatGiB(m.bytes)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    {#if timingMetrics.length > 0}
      <h2>Timing rounds</h2>
      <div class="run-table-wrap">
        <table class="run-table">
          <thead>
            <tr>
              <th>Round</th>
              <th class="run-table-num">#</th>
              <th class="run-table-num">TTFT</th>
              <th class="run-table-num">Decode</th>
              <th class="run-table-num">Prompt tok</th>
              <th class="run-table-num">Cache hit</th>
            </tr>
          </thead>
          <tbody>
            {#each timingMetrics as m (m.id)}
              <tr>
                <td>{m.roundType ?? "—"}</td>
                <td class="run-table-num">{formatCount(m.roundIndex)}</td>
                <td class="run-table-num">{formatMs(m.firstGenerationTokenMs)}</td>
                <td class="run-table-num">{formatTokS(m.observedDecodeTokS)}</td>
                <td class="run-table-num">{formatCount(m.promptTokens)}</td>
                <td class="run-table-num">{formatPct(m.cacheHitRatio)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    <h2>Trials</h2>
    {#if detail.trials.length === 0}
      <p class="run-empty">No trial rows on this experiment (a timing-only `full`-CLI record has none).</p>
    {:else}
      <div class="run-table-wrap">
        <table class="run-table">
          <thead>
            <tr>
              <th>Type</th>
              <th>Verdict</th>
              <th>Shape</th>
              <th>Task</th>
              <th class="run-table-num">Position</th>
              <th class="run-table-num">Prompt tok</th>
              <th class="run-table-num">Completion tok</th>
              <th>Finish</th>
              <th>Flags</th>
            </tr>
          </thead>
          <tbody>
            {#each detail.trials as trial (trial.id)}
              {@const flags = extractClassifierFlags(trial.raw)}
              <tr>
                <td>{trial.type}</td>
                <td>
                  {#if trial.classifierVerdict !== null}
                    <span class="pill {verdictPillClass(trial.classifierVerdict)}">{verdictLabel(trial.classifierVerdict)}</span>
                  {:else}
                    <span class="run-table-muted">—</span>
                  {/if}
                </td>
                <td>{trial.classifierShape ?? "—"}</td>
                <td>{trial.task ?? "—"}</td>
                <td class="run-table-num">{trial.positionFraction === null ? "—" : formatPct(trial.positionFraction)}</td>
                <td class="run-table-num">{formatCount(trial.promptTokens)}</td>
                <td class="run-table-num">{formatCount(trial.completionTokens)}</td>
                <td>{trial.finishReason ?? "—"}</td>
                <td class="run-table-flags">{formatClassifierFlags(flags) || "—"}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {/if}
{:else}
  <!-- ---- List ------------------------------------------------------------------------ -->
  <p class="eyebrow">Bake-off records</p>
  <h1>Runs</h1>
  <p class="run-lede">
    Ingested experiments from <code>runs/bakeoff/</code>, one row per <code>config.json</code>. Open a row for the
    per-trial verdicts and metrics.
  </p>

  {#if loading}
    <p>Loading…</p>
  {:else if error}
    <p class="error-text">{error}</p>
  {:else if experiments.length === 0}
    <p class="run-empty">
      No experiments have been ingested yet. Run <code>bun run ingest</code> against <code>runs/bakeoff/</code> (or, for
      dev, against the API fixtures: <code>bun run ingest -- --dir apps/api/fixtures/records</code>).
    </p>
  {:else}
    <div class="run-list">
      {#each experiments as exp (exp.id)}
        <a
          class="row run-row"
          href={runDetailPath(exp.id)}
          onclick={(event) => guardedNavClick(event, () => onopenrun(exp.id))}
        >
          <div class="run-row-main">
            <strong class="run-row-id">{exp.sweepCell?.alias ?? exp.id}</strong>
            <span class="run-row-sub">{exp.lineage.hfRepo ?? "unknown repo"} &middot; {exp.lineage.filename ?? "unknown file"}</span>
          </div>
          <div class="run-row-meta">
            <span class="pill pill-tag">{exp.trialCount} trials</span>
            <span class="pill pill-tag">{exp.metricCount} metrics</span>
            {#if exp.hasWilsonSummary}<span class="pill pill-ok">Wilson</span>{/if}
            <span class="run-row-time">{formatTimestamp(exp.timestamp)}</span>
          </div>
        </a>
      {/each}
    </div>
  {/if}
{/if}
