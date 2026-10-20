<script lang="ts">
  // Serving -- live-served aliases joined to their best measured cell (plan WP-2 bullet 5,
  // `bench-dashboard-polish.md`), and now the default landing view (plan WP-2, `capability-
  // surfacing-and-local-agent-endpoints.md`). `GET /api/serving` is a server-side reader of
  // `llama/config.yaml`; the SPA never reads any host file directly (Decisions locked
  // "Read-only over serving config"). The join to `experiments` is client-side
  // (`lib/serving/matchMeasured.ts`) -- most aliases have no measurement yet, which is an
  // expected, designed state, not an error. The "Capabilities" column renders
  // `row.alias.capability` (the per-model `metadata:` block, `docs/model-capabilities.md`)
  // ALONGSIDE the pre-existing Context/KV K/KV V/FA columns and the full-flag expand row
  // below -- augmenting, never subsetting or replacing either (a `null` capability, e.g. a
  // model predating the WP-1 metadata rollout, renders its own muted "no capability
  // metadata" note rather than an empty cell).
  //
  // "MTP" / "Prefill tok/s (p50/p90)" / "Decode tok/s (p50/p90)" columns (WP-1, `plans/
  // served-recipe-p50-prefill-and-decode.md` -- revises WP-3's `bakeoff-timing-records-and-
  // estimates.md` original single "tok/s (p50)" column) fetch the SEPARATE DB-backed `GET
  // /api/serving/timing` aggregate alongside `GET /api/serving` and join the two
  // client-side by `modelKey` (`lib/serving/matchTiming.ts`) -- `GET /api/serving` itself
  // stays a pure config-file parse, untouched. p50 AND p90 for both prefill and decode,
  // each with its own arm/sample-count provenance; a served model with no recipe-matched
  // timing data (the `bge-*` embed/rerank aliases, or any model whose only measured cells
  // are off-recipe) renders the literal "—", never a guessed value. The `MTP` pill is
  // driven by `capability.serving.mtp` (config truth) with the recipe's `specType` as
  // title/detail text (`lib/serving/capability.ts mtpDetail`) -- the product-surface label
  // is always "MTP", never the underlying method name (`draft-mtp` today).
  //
  // Visibility chips (WP-4, `plans/bench-dashboard-serving-control.md`): two per row, `cc`
  // and `openwebui`, whose ONLY source is `GET /api/serving/status`'s `visibility` map -- the
  // host dispatcher's republication of the model catalog's visibility (`router/catalog/catalog.json`), never this view's YAML parse
  // (which carries no visibility at all). Toggling POSTs the full desired `consumers` array
  // for that model, which only RECORDS a request; the chip enters an optimistic "requested"
  // state keyed by the returned `requestId` and settles when a later status shows that id in
  // `recent[]`. An unavailable status -- unset, unreadable, empty or stale -- disables the
  // chips with the reason in words rather than guessing. No restart banner: nothing on this
  // path requires one. All derivations live in `lib/serving/visibility.ts`; this file stays
  // markup-thin, and owns NO timer (refresh is `refreshToken`, CC-06/07).
  import type { ExperimentSummary, ServingStatusResponse, ServingTimingRow } from "@bench-dashboard/shared";
  import { getServing, getServingStatus, getServingTiming, postServingVisibility } from "../api.ts";
  import {
    rowChips,
    settlePending,
    unavailableReasonText,
    visibilityEntry,
    visibilityRequestBody,
    type PendingVisibilityMap,
    type ServingChipState,
  } from "../lib/serving/visibility.ts";
  import { matchServingToMeasured } from "../lib/serving/matchMeasured.ts";
  import { formatServingDecodeCell, formatServingPrefillCell, matchServingTiming } from "../lib/serving/matchTiming.ts";
  import { expandToggleLabel, flagValueClass, formatFlagValue, toggleExpandedRow } from "../lib/serving/expand.ts";
  import {
    capabilityReasoningLabel,
    capabilityReasoningPillClass,
    capabilityToolsLabel,
    capabilityToolsPillClass,
    formatCapabilityServingSummary,
    mtpDetail,
    mtpLabel,
    mtpPillClass,
  } from "../lib/serving/capability.ts";
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
  let timingRows = $state<ServingTimingRow[]>([]);
  let servingLoading = $state(false);
  let servingError = $state("");

  // Visibility control state. `servingStatus` stays `null` until a status response parses --
  // null is "unknown", and the chips render disabled rather than inventing a value.
  // `pendingVisibility` is the optimistic layer, keyed by `modelKey`.
  let servingStatus = $state<ServingStatusResponse | null>(null);
  let statusError = $state("");
  let visibilityError = $state("");
  let pendingVisibility = $state<PendingVisibilityMap>({});
  // A plain array, not a `Set`: `svelte/prefer-svelte-reactivity` forbids a mutable built-in
  // `Set` inside a component, and this is a two-element-at-most in-flight guard anyway.
  let busyKeys = $state<string[]>([]);

  // Per-row expand/collapse for the full serving config (WP-1) -- collapsed by default,
  // keyed by `modelKey` (same key the `{#each}` below uses). `lib/serving/expand.ts` owns
  // the toggle logic and the CC-04 label pair as plain, tested functions.
  let expandedRows = $state<Set<string>>(new Set());

  function toggleRow(modelKey: string) {
    expandedRows = toggleExpandedRow(expandedRows, modelKey);
  }

  // `Promise.allSettled`, the Router.svelte pattern: the visibility status is an INDEPENDENT
  // feed, so a status failure never blanks the served-alias table (and vice versa). The
  // config/timing pair keeps its previous all-or-nothing error behaviour.
  async function loadServing(exps: ExperimentSummary[]) {
    servingLoading = true;
    servingError = "";
    const [servingResult, timingResult, statusResult] = await Promise.allSettled([
      getServing(),
      getServingTiming(),
      getServingStatus(),
    ]);
    if (servingResult.status === "fulfilled" && timingResult.status === "fulfilled") {
      configPath = servingResult.value.configPath;
      rows = matchServingToMeasured(servingResult.value.models, exps);
      timingRows = timingResult.value;
    } else {
      const failure = servingResult.status === "rejected" ? servingResult.reason : (timingResult as PromiseRejectedResult).reason;
      servingError = failure instanceof Error ? failure.message : "Unable to load served aliases";
    }
    if (statusResult.status === "fulfilled") {
      servingStatus = statusResult.value;
      statusError = "";
      // Any request the dispatcher has now dispositioned stops being "requested".
      pendingVisibility = settlePending(pendingVisibility, servingStatus);
    } else {
      statusError =
        statusResult.reason instanceof Error ? statusResult.reason.message : "Unable to load the visibility status";
    }
    servingLoading = false;
  }

  /** Toggling a chip POSTs the FULL desired `consumers` array for that model, built from the
   * DECLARED array (never from the chip's rendering), and records the returned `requestId` as
   * this row's optimistic pending state. `accepted` means recorded, never executed. */
  async function toggleVisibility(modelKey: string, chip: ServingChipState) {
    if (chip.disabled || busyKeys.includes(modelKey)) return;
    const entry = visibilityEntry(servingStatus, modelKey);
    if (entry === null) return;
    const body = visibilityRequestBody(modelKey, entry, chip.consumer, !chip.on);
    visibilityError = "";
    busyKeys = [...busyKeys, modelKey];
    try {
      const accepted = await postServingVisibility(body);
      pendingVisibility = { ...pendingVisibility, [modelKey]: { requestId: accepted.requestId, consumers: body.consumers } };
    } catch (e) {
      visibilityError = e instanceof Error ? e.message : "Unable to record the visibility change";
    } finally {
      busyKeys = busyKeys.filter((key) => key !== modelKey);
    }
  }

  // One line explaining why every chip is inert, when that is the case -- the per-chip
  // tooltip carries the same reason, but a table of uniformly dead chips deserves a sentence.
  const visibilityNote = $derived(
    statusError !== ""
      ? statusError
      : servingStatus !== null && !servingStatus.available
        ? `Visibility controls are read-only: ${unavailableReasonText(servingStatus.reason)}.`
        : "",
  );

  // ONE effect tracking both `experiments` and `refreshToken` -- see Runs.svelte's own
  // comment on why this isn't split into two effects.
  $effect(() => {
    // eslint-disable-next-line no-unused-expressions -- bare read registers the $effect dependency
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

{#if visibilityNote}
  <p class="run-table-muted">{visibilityNote}</p>
{/if}
{#if visibilityError}
  <p class="error-text">{visibilityError}</p>
{/if}

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
          <th>Capabilities</th>
          <th>Visibility</th>
          <th>MTP</th>
          <th class="run-table-num">Prefill tok/s (p50/p90)</th>
          <th class="run-table-num">Decode tok/s (p50/p90)</th>
          <th>Measured cell</th>
          <th>Config</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as row (row.alias.modelKey)}
          {@const timingRow = matchServingTiming(row.alias.modelKey, timingRows)}
          <tr>
            <td class="serving-aliases">{row.alias.aliases.join(", ")}</td>
            <td>{row.alias.name ?? row.alias.modelKey}</td>
            <td class="run-table-num">{formatCount(row.alias.flags.contextLength)}</td>
            <td>{row.alias.flags.kvCacheTypeK ?? "—"}</td>
            <td>{row.alias.flags.kvCacheTypeV ?? "—"}</td>
            <td>{row.alias.flags.flashAttention ?? "—"}</td>
            <td class="serving-capabilities">
              {#if row.alias.capability}
                <div class="serving-cap-modalities">
                  {#each row.alias.capability.inputModalities as modality (modality)}
                    <span class="pill pill-tag">{modality}</span>
                  {/each}
                  <span class="serving-cap-arrow" aria-hidden="true">&rarr;</span>
                  {#each row.alias.capability.outputModalities as modality (modality)}
                    <span class="pill pill-tag">{modality}</span>
                  {/each}
                </div>
                <div class="serving-cap-facts">
                  <span class="pill {capabilityToolsPillClass(row.alias.capability.toolCalling)}">
                    {capabilityToolsLabel(row.alias.capability.toolCalling)}
                  </span>
                  <span class="pill {capabilityReasoningPillClass(row.alias.capability.reasoning)}">
                    {capabilityReasoningLabel(row.alias.capability.reasoning)}
                  </span>
                </div>
                <div class="serving-cap-serving run-table-muted">{formatCapabilityServingSummary(row.alias.capability)}</div>
              {:else}
                <span class="run-table-muted">no capability metadata</span>
              {/if}
            </td>
            <td>
              <div class="serving-visibility">
                {#each rowChips(row.alias.modelKey, servingStatus, pendingVisibility, statusError) as chip (chip.consumer)}
                  <button
                    type="button"
                    class="pill serving-chip {chip.pillClass}"
                    disabled={chip.disabled || busyKeys.includes(row.alias.modelKey)}
                    aria-pressed={chip.on}
                    title={chip.title}
                    onclick={() => void toggleVisibility(row.alias.modelKey, chip)}
                  >
                    {chip.label} {chip.stateLabel}
                  </button>
                {/each}
              </div>
            </td>
            <td>
              <span class="pill {mtpPillClass(row.alias.capability?.serving.mtp ?? null)}" title={mtpDetail(row.alias.recipe)}>
                {mtpLabel(row.alias.capability?.serving.mtp ?? null)}
              </span>
            </td>
            <td class="run-table-num">{formatServingPrefillCell(timingRow)}</td>
            <td class="run-table-num">{formatServingDecodeCell(timingRow)}</td>
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
            <td>
              <button
                class="btn-secondary"
                aria-expanded={expandedRows.has(row.alias.modelKey)}
                onclick={() => toggleRow(row.alias.modelKey)}
              >
                {expandToggleLabel(expandedRows.has(row.alias.modelKey))}
              </button>
            </td>
          </tr>
          {#if expandedRows.has(row.alias.modelKey)}
            <tr class="serving-flags-row">
              <td colspan="13">
                {#if row.alias.flags.allFlags.length === 0}
                  <p class="run-table-muted">No flags found in the served command.</p>
                {:else}
                  <table class="run-table serving-flags-table">
                    <thead>
                      <tr>
                        <th>Flag</th>
                        <th>Value</th>
                      </tr>
                    </thead>
                    <tbody>
                      {#each row.alias.flags.allFlags as entry, index (index)}
                        <tr>
                          <td><code>{entry.flag}</code></td>
                          <td class={flagValueClass(entry.value)}>{formatFlagValue(entry.value)}</td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                {/if}
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
  </div>
{/if}
