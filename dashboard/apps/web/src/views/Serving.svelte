<script lang="ts">
  // Serving view. Consumes `GET /api/serving` (config parse), `GET /api/serving/facts` (one
  // serving-facts-v1 generation: timing, measured, availability), `/status` (visibility
  // chips) and `/live` (residency), all in one `Promise.allSettled` per refresh; only the config
  // failing blanks the table. Derivations live in `lib/serving/*`; this file is markup-thin
  // and owns no timer (refresh is `refreshToken`).
  import type { ServingLiveResponse, ServingStatusResponse, ServingTimingRow } from "@bench-dashboard/shared";
  import {
    getServing,
    getServingFacts,
    getServingLive,
    getServingStatus,
    postServingVisibility,
  } from "../api.ts";
  import { researchRunUrl, resolveServingLoad } from "../lib/serving/facts.ts";
  import { TIER_LABELS, eventPill, liveUnavailableMessage, recentEvents, residencyDetail, residentPill, tierView } from "../lib/serving/live.ts";
  import { formatAge } from "../lib/router/format.ts";
  import {
    rowChips,
    settlePending,
    unavailableReasonText,
    visibilityEntry,
    visibilityRequestBody,
    type PendingVisibilityMap,
    type ServingChipState,
  } from "../lib/serving/visibility.ts";
  import type { ServingMatchRow } from "../lib/serving/matchMeasured.ts";
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
  import { formatCount } from "../lib/format.ts";

  let { refreshToken }: { refreshToken: number } = $props();

  let configPath = $state("");
  let rows = $state<ServingMatchRow[]>([]);
  let timingRows = $state<ServingTimingRow[]>([]);
  let servingLoading = $state(true);
  let servingError = $state("");
  // Timing/measured unavailable (facts file off, absent, invalid, stale-config, or a failed
  // fetch) -- shown in words above the table, never blanking it.
  let factsNote = $state("");
  let factsGeneratedAt = $state<string | null>(null);
  let researchPort = $state<number | null>(null);
  // Live residency (WP-6): an independent feed -- its failure never blanks the alias table.
  let live = $state<ServingLiveResponse | null>(null);
  let liveError = $state("");
  let asOf = $state(Date.now());

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
  // feed, so a status failure never blanks the served-alias table (and vice versa). Only the
  // serving config failing withholds the table; the facts degrade to a note
  // (`lib/serving/facts.ts resolveServingLoad`).
  async function loadServing() {
    servingError = "";
    const [servingResult, factsResult, statusResult, liveResult] = await Promise.allSettled([
      getServing(),
      getServingFacts(),
      getServingStatus(),
      getServingLive(),
    ]);
    if (liveResult.status === "fulfilled") {
      live = liveResult.value;
      liveError = "";
    } else {
      liveError = liveResult.reason instanceof Error ? liveResult.reason.message : "Unable to load live residency";
    }
    asOf = Date.now();
    const load = resolveServingLoad(servingResult, factsResult);
    timingRows = load.timingRows;
    factsNote = load.factsNote;
    factsGeneratedAt = load.factsGeneratedAt;
    researchPort = load.researchPort;
    servingError = load.servingError;
    if (load.rows !== null) {
      rows = load.rows;
      configPath = load.configPath ?? "";
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

  $effect(() => {
    // eslint-disable-next-line no-unused-expressions -- bare read registers the $effect dependency
    refreshToken;
    void loadServing();
  });
</script>

<p class="eyebrow">Live vs. measured</p>
<h1>Serving</h1>
<p class="run-lede">
  Aliases currently served by the llama router, joined to their most recent measured cell where one exists.
  {#if configPath}<code>{configPath}</code>{/if}
</p>

<section class="router-panel">
  <div class="router-panel-head">
    <h2>Live residency</h2>
    {#if live !== null && live.available}<span class="router-ambient">as of {formatAge(live.generated_at, asOf)}</span>{/if}
  </div>
  {#if liveError}<p class="error-text">{liveError}</p>{/if}
  {#if live === null}
    <p class="serving-empty">No residency feed has been read yet.</p>
  {:else if !live.available}
    <p class="serving-empty">Live residency unavailable — {liveUnavailableMessage(live.reason)}.</p>
  {:else}
    {#each [["gpu", live.gpu], ["onnx", live.onnx]] as const as [tier, status] (tier)}
      {@const view = tierView(status)}
      <h3>
        {TIER_LABELS[tier]}
        {#if view.sampledAt !== null}<span class="router-ambient">sampled {formatAge(view.sampledAt, asOf)}</span>{/if}
      </h3>
      {#if view.note}<p class="serving-empty">{view.note}</p>{/if}
      {#if view.models.length > 0}
        <ul>
          {#each view.models as m (m.model)}
            {@const pill = residentPill(m)}
            <li><code>{m.model}</code> <span class="pill {pill.cls}">{pill.label}</span> <span class="router-ambient">{residencyDetail(m)}</span></li>
          {/each}
        </ul>
      {/if}
    {/each}
    <h3>Recent loads / unloads</h3>
    {#if live.recent.length === 0}
      <p class="serving-empty">No transitions in the feed window.</p>
    {:else}
      <ul>
        {#each recentEvents(live.recent) as ev, index (`${ev.ts}#${ev.tier}#${ev.model}#${index}`)}
          {@const pill = eventPill(ev)}
          <li><span class="pill {pill.cls}">{pill.label}</span> <code>{ev.model}</code> <span class="router-ambient">{TIER_LABELS[ev.tier]} · {formatAge(ev.ts, asOf)}</span></li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>

{#if visibilityNote}
  <p class="serving-table-muted">{visibilityNote}</p>
{/if}
{#if visibilityError}
  <p class="error-text">{visibilityError}</p>
{/if}

{#if factsNote}
  <p class="serving-empty">{factsNote}</p>
{:else if factsGeneratedAt}
  <p class="serving-table-muted">Timing and measured facts as of <code>{factsGeneratedAt}</code></p>
{/if}

{#if servingLoading && rows.length === 0 && !servingError}
  <p>Loading…</p>
{:else if servingError}
  <p class="error-text">{servingError}</p>
{:else if rows.length === 0}
  <p class="serving-empty">
    No aliased models resolved from the served config -- either <code>LLAMA_CONFIG_PATH</code> is unset, or the
    config has no models carrying an <code>aliases:</code> list.
  </p>
{:else}
  <div class="serving-table-wrap">
    <table class="serving-table">
      <thead>
        <tr>
          <th>Aliases</th>
          <th>Name</th>
          <th class="serving-table-num">Context</th>
          <th>KV K</th>
          <th>KV V</th>
          <th>FA</th>
          <th>Capabilities</th>
          <th>Visibility</th>
          <th>MTP</th>
          <th class="serving-table-num">Prefill tok/s (p50/p90)</th>
          <th class="serving-table-num">Decode tok/s (p50/p90)</th>
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
            <td class="serving-table-num">{formatCount(row.alias.flags.contextLength)}</td>
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
                <div class="serving-cap-serving serving-table-muted">{formatCapabilityServingSummary(row.alias.capability)}</div>
              {:else}
                <span class="serving-table-muted">no capability metadata</span>
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
            <td class="serving-table-num">{formatServingPrefillCell(timingRow)}</td>
            <td class="serving-table-num">{formatServingDecodeCell(timingRow)}</td>
            <td>
              {#if row.measured && researchPort !== null}
                <a class="serving-measured-link" href={researchRunUrl(location, researchPort, row.measured.experimentId)}>
                  {row.measured.alias}
                </a>
              {:else}
                <span class="serving-table-muted">no measurement yet</span>
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
                  <p class="serving-table-muted">No flags found in the served command.</p>
                {:else}
                  <table class="serving-table serving-flags-table">
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
