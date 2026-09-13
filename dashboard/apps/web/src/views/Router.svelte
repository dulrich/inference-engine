<script lang="ts">
  // Router -- the model router's read-only status plane (WP-4, `plans/router-observability-
  // and-tool-search-conformance.md`), over the three feeds WP-3 froze in
  // `packages/shared/src/routerStatus.ts`. Everything here is FILE-derived: the dashboard API
  // makes zero outbound HTTP, the slot samples come from the host poller unit, and this view
  // adds no clock of its own -- refresh is App.svelte's single PollControl, arriving as
  // `refreshToken` (CC-06/07), exactly the `{refreshToken}`-only shape Queue.svelte uses.
  //
  // CC-14 render stability: `loading` flips once per fetch, the three payloads are held
  // INDEPENDENTLY (one feed failing never blanks the other two -- `Promise.allSettled`), and
  // a payload is only ever replaced by a parsed successor, so a background poll can never
  // flash a placeholder over data that is already on screen.
  import type { RouterSidecarResponse, RouterSlotsResponse, RouterTelemetryResponse } from "@bench-dashboard/shared";
  import { getRouterSidecar, getRouterSlots, getRouterTelemetry } from "../api.ts";
  import { formatAge, formatDurationMs, sessionLabel, truncateId, truncateLine } from "../lib/router/format.ts";
  import {
    ALL_LEGS,
    applyLegFilter,
    isErrorRecord,
    legChips,
    legFilterOptions,
    legKey,
    newestFirst,
    resolveLegFilter,
  } from "../lib/router/legs.ts";
  import { elapsedLabel, phasePill, splitLabel, tokPerSecLabel, tokenSplit } from "../lib/router/inflight.ts";
  import { cacheRatioLabel, modelStatePill, processingPill, recipeFlags, sampleAgeLabel, unavailableMessage } from "../lib/router/slots.ts";
  import { formatCount } from "../lib/format.ts";

  let { refreshToken }: { refreshToken: number } = $props();

  let telemetry = $state<RouterTelemetryResponse | null>(null);
  let slots = $state<RouterSlotsResponse | null>(null);
  let sidecar = $state<RouterSidecarResponse | null>(null);
  let telemetryError = $state("");
  let slotsError = $state("");
  let sidecarError = $state("");
  let loading = $state(false);
  let loaded = $state(false);
  // The age baseline, stamped once per fetch -- ages are ambient readouts that move with the
  // poll, never with a view-local ticker.
  let asOf = $state(Date.now());
  let legFilter = $state<string>(ALL_LEGS);

  function reason(error: unknown, fallback: string): string {
    return error instanceof Error ? error.message : fallback;
  }

  async function loadRouter() {
    loading = true;
    const [telemetryResult, slotsResult, sidecarResult] = await Promise.allSettled([
      getRouterTelemetry(),
      getRouterSlots(),
      getRouterSidecar(),
    ]);
    if (telemetryResult.status === "fulfilled") {
      telemetry = telemetryResult.value;
      telemetryError = "";
    } else {
      telemetryError = reason(telemetryResult.reason, "Unable to load router telemetry");
    }
    if (slotsResult.status === "fulfilled") {
      slots = slotsResult.value;
      slotsError = "";
    } else {
      slotsError = reason(slotsResult.reason, "Unable to load slot samples");
    }
    if (sidecarResult.status === "fulfilled") {
      sidecar = sidecarResult.value;
      sidecarError = "";
    } else {
      sidecarError = reason(sidecarResult.reason, "Unable to load the codex sidecar summary");
    }
    asOf = Date.now();
    loaded = true;
    loading = false;
  }

  $effect(() => {
    refreshToken;
    void loadRouter();
  });

  // Narrowing helpers: each response is a discriminated union on `available`, and a function
  // return narrows where an inline optional-chained check would not.
  function availableTelemetry(value: RouterTelemetryResponse | null) {
    return value !== null && value.available ? value : null;
  }
  function availableSlots(value: RouterSlotsResponse | null) {
    return value !== null && value.available ? value : null;
  }
  function availableSidecar(value: RouterSidecarResponse | null) {
    return value !== null && value.available ? value : null;
  }

  const tele = $derived(availableTelemetry(telemetry));
  const slotData = $derived(availableSlots(slots));
  const sidecarData = $derived(availableSidecar(sidecar));

  const legOptions = $derived(tele === null ? [ALL_LEGS] : legFilterOptions(tele.legs));
  const activeLeg = $derived(resolveLegFilter(legFilter, legOptions));
  const chips = $derived(tele === null ? [] : legChips(tele.legs));
  // Reverse to newest-first ONCE, in the lib helper, then filter -- never in the markup.
  const recentRows = $derived(tele === null ? [] : applyLegFilter(newestFirst(tele.recent), activeLeg));
</script>

<p class="eyebrow">Model router</p>
<h1>Router</h1>
<p class="run-lede">
  Read-only status for the inference router, derived entirely from files: the router's own request sink
  (<code>/api/router/telemetry</code>), the host slot poller's samples (<code>/api/router/slots</code>), and the codex
  sidecar's log (<code>/api/router/sidecar</code>). Each feed reports its own availability -- one being off never
  hides the other two. Refresh interval is the header's poll control.
</p>

{#if !loaded && loading}
  <p>Loading…</p>
{:else}
  {#if tele !== null}
    <p class="queue-generated router-ambient">telemetry as of {formatAge(tele.generatedAt, asOf)} ({tele.file})</p>
  {/if}

  <section class="router-panel">
    <div class="router-panel-head">
      <h2>Cache &amp; slots</h2>
      {#if slotData !== null}
        <span class="router-ambient">{sampleAgeLabel(slotData.ageMs)}</span>
        {#if slotData.stale}<span class="pill pill-warn">stale</span>{/if}
      {/if}
    </div>
    {#if slotsError}<p class="error-text">{slotsError}</p>{/if}
    {#if slots === null}
      <p class="run-empty">No slot sample has been read yet.</p>
    {:else if !slots.available}
      <p class="run-empty">Slot sampling unavailable — {unavailableMessage(slots.reason)}.</p>
    {:else if slots.models.length === 0}
      <p class="run-empty">The newest sample lists no models.</p>
    {:else}
      <div class="router-card-list">
        {#each slots.models as model (model.model)}
          {@const statePill = modelStatePill(model.state)}
          <div class="router-card">
            <div class="router-card-head">
              <span class="router-card-name">{model.model}</span>
              <span class="pill {statePill.cls}">{statePill.label}</span>
            </div>
            <div class="router-meta-line router-ambient">
              <span>ttl {formatCount(model.ttl)}</span>
              <span>n_ctx {formatCount(model.nCtx)}</span>
              {#each recipeFlags(model.recipe) as flag (flag.label)}
                <span>{flag.label} {flag.value}</span>
              {/each}
            </div>
            {#if model.error}<p class="error-text">{model.error}</p>{/if}
            {#if model.slots.length === 0}
              <p class="run-empty">No slots sampled — only models the router reports as ready are ever polled.</p>
            {:else}
              <div class="run-table-wrap">
                <table class="run-table">
                  <thead>
                    <tr>
                      <th>Slot</th>
                      <th>State</th>
                      <th class="run-table-num">n_ctx</th>
                      <th class="run-table-num">Prompt tok</th>
                      <th class="run-table-num">Cached tok</th>
                      <th class="run-table-num">Cache hit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each model.slots as slot, index (`${model.model}#${slot.id ?? index}`)}
                      {@const busy = processingPill(slot.isProcessing)}
                      <tr>
                        <td>{formatCount(slot.id)}</td>
                        <td><span class="pill {busy.cls}">{busy.label}</span></td>
                        <td class="run-table-num">{formatCount(slot.nCtx)}</td>
                        <td class="run-table-num">{formatCount(slot.promptTokens)}</td>
                        <td class="run-table-num">{formatCount(slot.promptTokensCache)}</td>
                        <td class="run-table-num">{cacheRatioLabel(slot)}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </section>

  <section class="router-panel">
    <div class="router-panel-head">
      <h2>In-flight generations</h2>
      {#if tele !== null}<span class="router-ambient">{tele.inFlight.length} live</span>{/if}
    </div>
    {#if telemetryError}<p class="error-text">{telemetryError}</p>{/if}
    {#if telemetry === null}
      <p class="run-empty">No telemetry has been read yet.</p>
    {:else if !telemetry.available}
      <p class="run-empty">Router telemetry unavailable — {unavailableMessage(telemetry.reason)}.</p>
    {:else if telemetry.inFlight.length === 0}
      <p class="run-empty">No live generations.</p>
    {:else}
      <div class="run-table-wrap">
        <table class="run-table">
          <thead>
            <tr>
              <th>Leg</th>
              <th>Model</th>
              <th>Session</th>
              <th>Phase</th>
              <th class="run-table-num">Elapsed</th>
              <th>Reasoning / response</th>
              <th class="run-table-num">Rate</th>
            </tr>
          </thead>
          <tbody>
            {#each telemetry.inFlight as row (row.reqId)}
              {@const phase = phasePill(row.phase)}
              {@const split = tokenSplit(row.reasoningTokens, row.responseTokens)}
              <tr>
                <td>{legKey(row.leg)}</td>
                <td>{row.modelResolved ?? "—"}</td>
                <td><span title={row.sessionId ?? "no session id on this request"}>{sessionLabel(row.sessionId)}</span></td>
                <td><span class="pill {phase.cls}">{phase.label}</span></td>
                <td class="run-table-num">{elapsedLabel(row.elapsedMs)}</td>
                <td>
                  <div class="router-split" aria-hidden="true">
                    <span class="router-split-reasoning" style="width: {split.reasoningPct}%"></span>
                    <span class="router-split-response" style="width: {split.responsePct}%"></span>
                  </div>
                  <span class="router-ambient">{splitLabel(split, row.reasoningTokens, row.responseTokens)}</span>
                </td>
                <td class="run-table-num">{tokPerSecLabel(row.tokPerSec)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>

  <section class="router-panel">
    <div class="router-panel-head">
      <h2>Legs &amp; sidecar</h2>
      {#if tele !== null}<span class="router-ambient">{recentRows.length} of {tele.recent.length} requests</span>{/if}
    </div>
    {#if telemetry !== null && telemetry.available}
      <div class="router-chips">
        {#each chips as chip (chip.key)}
          <span class="pill {chip.pillClass}">
            {chip.label}: {chip.requests} req / {chip.errors} err / p50 TTFT {chip.p50Label}
          </span>
        {/each}
      </div>
      <div class="seg-group" role="group" aria-label="Filter requests by leg">
        {#each legOptions as option (option)}
          <button
            type="button"
            class="seg"
            class:active={activeLeg === option}
            aria-pressed={activeLeg === option}
            onclick={() => (legFilter = option)}
          >
            {option}
          </button>
        {/each}
      </div>
      {#if recentRows.length === 0}
        <p class="run-empty">No requests in this window for the selected leg.</p>
      {:else}
        <div class="run-table-wrap">
          <table class="run-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Leg</th>
                <th>Model</th>
                <th>Status</th>
                <th>Stop reason</th>
                <th class="run-table-num">TTFT</th>
                <th class="run-table-num">Wall</th>
                <th class="run-table-num">In / out tok</th>
                <th>Session</th>
              </tr>
            </thead>
            <tbody>
              {#each recentRows as row (row.reqId)}
                <tr>
                  <td class="router-ambient">{formatAge(row.ts, asOf)}</td>
                  <td>{legKey(row.leg)}</td>
                  <td>{row.modelResolved ?? "—"}</td>
                  <td>
                    {formatCount(row.status)}
                    {#if isErrorRecord(row)}<span class="pill pill-error">error</span>{/if}
                  </td>
                  <td class="run-table-muted">{row.stopReason ?? "—"}</td>
                  <td class="run-table-num">{formatDurationMs(row.ttftMs)}</td>
                  <td class="run-table-num">{formatDurationMs(row.wallMs)}</td>
                  <td class="run-table-num">{formatCount(row.usage?.input ?? null)} / {formatCount(row.usage?.output ?? null)}</td>
                  <td>
                    <span title={row.sessionId ?? "no session id on this request"}>{sessionLabel(row.sessionId)}</span>
                    {#if row.isSubagent}<span class="pill pill-tag">subagent</span>{/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    {:else if telemetry === null}
      <p class="run-empty">No telemetry has been read yet.</p>
    {:else}
      <p class="run-empty">Router telemetry unavailable — {unavailableMessage(telemetry.reason)}.</p>
    {/if}

    <h3>Codex sidecar</h3>
    {#if sidecarError}<p class="error-text">{sidecarError}</p>{/if}
    {#if sidecar === null}
      <p class="run-empty">No sidecar log has been read yet.</p>
    {:else if !sidecar.available}
      <p class="run-empty">Sidecar log unavailable — {unavailableMessage(sidecar.reason)}.</p>
    {:else}
      {#if sidecarData !== null}
        <p class="router-ambient">as of {formatAge(sidecarData.generatedAt, asOf)} ({sidecarData.file})</p>
      {/if}
      {#if sidecar.personas.length === 0}
        <p class="run-empty">No persona activity in this window.</p>
      {:else}
        <div class="run-table-wrap">
          <table class="run-table">
            <thead>
              <tr>
                <th>Persona</th>
                <th>Thread</th>
                <th>Model</th>
                <th>Last event</th>
                <th>Seen</th>
              </tr>
            </thead>
            <tbody>
              {#each sidecar.personas as persona (persona.persona)}
                <tr>
                  <td>{persona.persona}</td>
                  <td><span title={persona.threadId ?? "no thread id"}>{truncateId(persona.threadId)}</span></td>
                  <td>{persona.model ?? "—"}</td>
                  <td>{persona.lastKind}</td>
                  <td class="router-ambient">{formatAge(persona.lastSeenTs, asOf)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
      {#if sidecar.recentErrors.length > 0}
        <ul class="router-error-list">
          {#each sidecar.recentErrors as entry, index (`${entry.ts}#${index}`)}
            <li>
              <span class="pill pill-error">{entry.kind}</span>
              <span class="router-ambient">{formatAge(entry.ts, asOf)}</span>
              <span>{entry.persona}</span>
              <span>{formatCount(entry.status)}</span>
              <span class="run-table-muted">{truncateLine(entry.message)}</span>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  </section>
{/if}
