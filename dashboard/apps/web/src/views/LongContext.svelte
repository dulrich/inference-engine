<script lang="ts">
  // Long-context -- reliability curves (plan WP-2 bullet 3). Primary: `GET
  // /api/curves/:id`'s failure-rate-vs-position points (server-computed, Wilson-scored) for
  // a picked experiment. Secondary: "failure rate vs real tokens", assembled client-side by
  // grouping every experiment's longcontext_probe trials (promptTokens from the detail
  // route) -- the plan's own pin, since no such route exists server-side. When NO experiment
  // has any longcontext_probe data (true of the live DB today), this renders a designed
  // empty state naming the deferred measurement campaign, never a blank panel or a crash.
  import type { CurveSeriesPoint, ExperimentSummary } from "@bench-dashboard/shared";
  import type uPlot from "uplot";
  import { getCurves, getExperimentDetail } from "../api.ts";
  import { buildCurveChartData } from "../lib/longcontext/curveChart.ts";
  import { taskSeriesColor } from "../lib/longcontext/chartData.ts";
  import { buildTokenBinChartData, groupByPromptTokens, type LongcontextTrialLike } from "../lib/longcontext/tokenBins.ts";
  import { formatCount, formatPct } from "../lib/format.ts";
  import UPlotChart from "../lib/uplot/UPlotChart.svelte";

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

  interface ExperimentCurve {
    experiment: ExperimentSummary;
    points: CurveSeriesPoint[];
  }

  let curvesByExperiment = $state<ExperimentCurve[]>([]);
  let curvesLoading = $state(false);
  let curvesError = $state("");
  let selectedExperimentId = $state<string | null>(null);

  let tokenTrials = $state<LongcontextTrialLike[]>([]);
  let tokenTrialsLoading = $state(false);
  let tokenTrialsError = $state("");

  async function loadCurves(exps: ExperimentSummary[]) {
    if (exps.length === 0) {
      curvesByExperiment = [];
      return;
    }
    curvesLoading = true;
    curvesError = "";
    try {
      const results = await Promise.all(exps.map(async (experiment) => ({ experiment, series: await getCurves(experiment.id) })));
      curvesByExperiment = results
        .filter((r) => r.series.points.length > 0)
        .map((r) => ({ experiment: r.experiment, points: r.series.points }));
    } catch (e) {
      curvesError = e instanceof Error ? e.message : "Unable to load reliability curves";
    } finally {
      curvesLoading = false;
    }
  }

  async function loadTokenTrials(exps: ExperimentCurve[]) {
    if (exps.length === 0) {
      tokenTrials = [];
      return;
    }
    tokenTrialsLoading = true;
    tokenTrialsError = "";
    try {
      const details = await Promise.all(exps.map((e) => getExperimentDetail(e.experiment.id)));
      tokenTrials = details.flatMap((detail) =>
        detail.trials
          .filter((t) => t.type === "longcontext_probe")
          .map((t) => ({ task: t.task, promptTokens: t.promptTokens, classifierVerdict: t.classifierVerdict })),
      );
    } catch (e) {
      tokenTrialsError = e instanceof Error ? e.message : "Unable to load per-trial token counts";
    } finally {
      tokenTrialsLoading = false;
    }
  }

  // ONE effect tracking both `experiments` and `refreshToken` -- see Runs.svelte's own
  // comment on why this isn't split into two effects.
  $effect(() => {
    // eslint-disable-next-line no-unused-expressions -- bare read registers the $effect dependency
    refreshToken;
    void loadCurves(experiments);
  });

  $effect(() => {
    if (curvesByExperiment.length > 0 && (selectedExperimentId === null || !curvesByExperiment.some((c) => c.experiment.id === selectedExperimentId))) {
      selectedExperimentId = curvesByExperiment[0]?.experiment.id ?? null;
    }
    if (curvesByExperiment.length === 0) selectedExperimentId = null;
  });

  $effect(() => {
    void loadTokenTrials(curvesByExperiment);
  });

  const selectedCurve = $derived(curvesByExperiment.find((c) => c.experiment.id === selectedExperimentId) ?? null);
  const curveChartData = $derived(selectedCurve ? buildCurveChartData(selectedCurve.points) : null);
  const tokenBins = $derived(groupByPromptTokens(tokenTrials));
  const tokenChartData = $derived(buildTokenBinChartData(tokenBins));

  function handleExperimentChange(event: Event) {
    selectedExperimentId = (event.currentTarget as HTMLSelectElement).value;
  }

  function buildCurveOptions(chartData: NonNullable<typeof curveChartData>) {
    return (token: (name: string) => string, width: number, height: number): uPlot.Options => ({
      width,
      height,
      cursor: { y: false },
      legend: { show: false },
      scales: { x: { time: false } },
      axes: [
        { stroke: token("--muted"), grid: { stroke: token("--line") }, values: (_u, splits) => splits.map((s) => (s === null ? "" : `${((s as number) * 100).toFixed(0)}%`)) },
        { stroke: token("--muted"), grid: { stroke: token("--line") }, values: (_u, splits) => splits.map((s) => (s === null ? "" : `${((s as number) * 100).toFixed(0)}%`)) },
      ],
      series: [
        {},
        ...chartData.tasks.map((task, i) => ({
          label: task,
          stroke: taskSeriesColor(i, token),
          width: 2,
          points: { show: true, size: 6 },
          spanGaps: true,
        })),
      ],
    });
  }

  function buildTokenOptions(chartData: typeof tokenChartData) {
    return (token: (name: string) => string, width: number, height: number): uPlot.Options => ({
      width,
      height,
      cursor: { y: false },
      legend: { show: false },
      scales: { x: { time: false } },
      axes: [
        { stroke: token("--muted"), grid: { stroke: token("--line") } },
        { stroke: token("--muted"), grid: { stroke: token("--line") }, values: (_u, splits) => splits.map((s) => (s === null ? "" : `${((s as number) * 100).toFixed(0)}%`)) },
      ],
      series: [
        {},
        ...chartData.tasks.map((task, i) => ({
          label: task,
          stroke: taskSeriesColor(i, token),
          width: 0,
          points: { show: true, size: 8 },
          spanGaps: false,
        })),
      ],
    });
  }
</script>

<p class="eyebrow">Reliability curves</p>
<h1>Long-context</h1>
<p class="run-lede">
  Failure rate vs. haystack position (per task, two-sided 95% Wilson bands) and vs. real measured prompt tokens,
  assembled from every experiment's <code>longcontext_probe</code> trials.
</p>

{#if loading || curvesLoading}
  <p>Loading…</p>
{:else if error}
  <p class="error-text">{error}</p>
{:else if curvesError}
  <p class="error-text">{curvesError}</p>
{:else if curvesByExperiment.length === 0}
  <p class="run-empty">
    No long-context reliability data has been measured yet. This view populates once the position-sweep + real-token
    measurement campaign (<code>llama/bakeoff/longcontext.py</code>) has produced <code>longcontext_probe</code>
    trials for at least one ingested experiment -- that campaign is deferred, not run yet, on the live tree.
  </p>
{:else}
  <div class="field ladders-picker">
    <label for="longcontext-experiment">Experiment</label>
    <select id="longcontext-experiment" value={selectedExperimentId} onchange={handleExperimentChange}>
      {#each curvesByExperiment as c (c.experiment.id)}
        <option value={c.experiment.id}>{c.experiment.sweepCell?.alias ?? c.experiment.id}</option>
      {/each}
    </select>
  </div>

  {#if selectedCurve && curveChartData}
    <h2>Failure rate vs. position</h2>
    <UPlotChart
      buildOptions={buildCurveOptions(curveChartData)}
      data={[curveChartData.xs, ...curveChartData.seriesValues]}
      ariaLabel="Failure rate vs. haystack position, one line per task"
    />
    <div class="run-table-wrap">
      <table class="run-table">
        <thead>
          <tr>
            <th>Task</th>
            <th class="run-table-num">Position</th>
            <th class="run-table-num">n</th>
            <th class="run-table-num">k</th>
            <th class="run-table-num">Failure rate</th>
            <th>Interval (sided, confidence)</th>
          </tr>
        </thead>
        <tbody>
          {#each selectedCurve.points as p (`${p.task}:${p.positionFraction}`)}
            <tr>
              <td>{p.task}</td>
              <td class="run-table-num">{formatPct(p.positionFraction)}</td>
              <td class="run-table-num">{formatCount(p.n)}</td>
              <td class="run-table-num">{formatCount(p.k)}</td>
              <td class="run-table-num">{formatPct(p.failureRate)}</td>
              <td>
                [{formatPct(p.lower)}, {formatPct(p.upper)}] ({p.sided === "one" ? "one-sided" : "two-sided"}, {formatPct(p.confidence)})
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <h2>Failure rate vs. real prompt tokens</h2>
  {#if tokenTrialsLoading}
    <p>Loading token bins…</p>
  {:else if tokenTrialsError}
    <p class="error-text">{tokenTrialsError}</p>
  {:else if tokenBins.length === 0}
    <p class="run-empty">
      No <code>longcontext_probe</code> trial in the ingested experiments carries a recorded real prompt-token count
      yet.
    </p>
  {:else}
    <UPlotChart
      buildOptions={buildTokenOptions(tokenChartData)}
      data={[tokenChartData.xs, ...tokenChartData.seriesValues]}
      ariaLabel="Failure rate vs. real prompt token count, one series per task"
    />
    <div class="run-table-wrap">
      <table class="run-table">
        <thead>
          <tr>
            <th>Task</th>
            <th class="run-table-num">Prompt tokens</th>
            <th class="run-table-num">n</th>
            <th class="run-table-num">k</th>
            <th class="run-table-num">Failure rate</th>
          </tr>
        </thead>
        <tbody>
          {#each tokenBins as bin (`${bin.task}:${bin.promptTokens}`)}
            <tr>
              <td>{bin.task}</td>
              <td class="run-table-num">{formatCount(bin.promptTokens)}</td>
              <td class="run-table-num">{formatCount(bin.n)}</td>
              <td class="run-table-num">{formatCount(bin.k)}</td>
              <td class="run-table-num">{formatPct(bin.failureRate)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
{/if}
