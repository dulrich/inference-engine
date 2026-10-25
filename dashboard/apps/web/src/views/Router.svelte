<script lang="ts">
  // Router -- the model router's read-only status plane (WP-4, `plans/router-observability-
  // and-tool-search-conformance.md`), over the three feeds WP-3 froze in
  // `packages/shared/src/routerStatus.ts`. Everything here is FILE-derived: the dashboard API
  // makes zero outbound HTTP (the slot panel retired at serving-control-redesign WP-6; live
  // residency is the Serving view's), and this view
  // adds no clock of its own -- refresh is App.svelte's single PollControl, arriving as
  // `refreshToken` (CC-06/07), exactly the `{refreshToken}`-only shape Queue.svelte uses.
  //
  // CC-14 render stability: `loading` flips once per fetch, the three payloads are held
  // INDEPENDENTLY (one feed failing never blanks the other two -- `Promise.allSettled`), and
  // a payload is only ever replaced by a parsed successor, so a background poll can never
  // flash a placeholder over data that is already on screen.
  //
  // Model catalog panel (WP-4, `plans/serving-control-redesign.md`): reads the host
  // dispatcher's projection of `router/catalog/catalog.json` from `GET /api/serving/status`
  // (the dashboard cannot read the catalog file) and edits it ONLY through drop-box intents
  // (`POST /api/catalog/{add,remove,consumers}`). A click records a request; the entry shows
  // "requested" until the status file's `recent[]` dispositions that id, and the panel head
  // shows the newest catalog outcome (applied revision, or the rejection/failure reason).
  // `lib/router/catalog.ts` owns every decision.
  import type {
    RouterSidecarResponse,
    RouterTelemetryResponse,
    ServingCatalogModel,
    ServingCatalogProvider,
    ServingConsumer,
    ServingStatusResponse,
  } from "@bench-dashboard/shared";
  import {
    getRouterSidecar,
    getRouterTelemetry,
    getServingStatus,
    postCatalogAdd,
    postCatalogConsumers,
    postCatalogRemove,
  } from "../api.ts";
  import {
    CATALOG_CONSUMERS,
    addVerdict,
    addableProviders,
    catalogBlock,
    chipVerdict,
    entryKey,
    isRouterInternal,
    latestCatalogOutcome,
    nextCatalogConsumers,
    outcomeFor,
    outcomePillClass,
    removeVerdict,
    uiConsumers,
  } from "../lib/router/catalog.ts";
  import { consumerLabel, unavailableReasonText } from "../lib/serving/visibility.ts";
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
  import { unavailableMessage } from "../lib/router/status.ts";
  import { formatCount } from "../lib/format.ts";

  let { refreshToken }: { refreshToken: number } = $props();

  let telemetry = $state<RouterTelemetryResponse | null>(null);
  let sidecar = $state<RouterSidecarResponse | null>(null);
  let telemetryError = $state("");
  let sidecarError = $state("");
  let loading = $state(false);
  let loaded = $state(false);
  // The age baseline, stamped once per fetch -- ages are ambient readouts that move with the
  // poll, never with a view-local ticker.
  let asOf = $state(Date.now());
  let legFilter = $state<string>(ALL_LEGS);

  // Catalog panel state. `servingStatus` stays `null` until a status parses (null = unknown,
  // every control disabled). `pendingCatalog` maps an entry key to the request id recorded for
  // it; an id leaves the map once `recent[]` dispositions it.
  let servingStatus = $state<ServingStatusResponse | null>(null);
  let statusError = $state("");
  let catalogError = $state("");
  let pendingCatalog = $state<Record<string, string>>({});
  let addProviderName = $state("");
  let addId = $state("");
  let addConsumers = $state<ServingConsumer[]>([]);
  let addBusy = $state(false);

  function reason(error: unknown, fallback: string): string {
    return error instanceof Error ? error.message : fallback;
  }

  function settleCatalog(status: ServingStatusResponse | null) {
    const next: Record<string, string> = {};
    let changed = false;
    for (const [key, requestId] of Object.entries(pendingCatalog)) {
      if (outcomeFor(status, requestId) !== null) {
        changed = true;
        continue;
      }
      next[key] = requestId;
    }
    if (changed) pendingCatalog = next;
  }

  async function loadRouter() {
    loading = true;
    const [telemetryResult, sidecarResult, statusResult] = await Promise.allSettled([
      getRouterTelemetry(),
      getRouterSidecar(),
      getServingStatus(),
    ]);
    if (statusResult.status === "fulfilled") {
      servingStatus = statusResult.value;
      statusError = "";
      settleCatalog(servingStatus);
    } else {
      statusError = reason(statusResult.reason, "Unable to load the serving status");
    }
    if (telemetryResult.status === "fulfilled") {
      telemetry = telemetryResult.value;
      telemetryError = "";
    } else {
      telemetryError = reason(telemetryResult.reason, "Unable to load router telemetry");
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
    // eslint-disable-next-line no-unused-expressions -- bare read registers the $effect dependency
    refreshToken;
    void loadRouter();
  });

  // Narrowing helpers: each response is a discriminated union on `available`, and a function
  // return narrows where an inline optional-chained check would not.
  function availableTelemetry(value: RouterTelemetryResponse | null) {
    return value !== null && value.available ? value : null;
  }
  function availableSidecar(value: RouterSidecarResponse | null) {
    return value !== null && value.available ? value : null;
  }

  const tele = $derived(availableTelemetry(telemetry));
  const sidecarData = $derived(availableSidecar(sidecar));

  const legOptions = $derived(tele === null ? [ALL_LEGS] : legFilterOptions(tele.legs));
  const activeLeg = $derived(resolveLegFilter(legFilter, legOptions));
  const chips = $derived(tele === null ? [] : legChips(tele.legs));
  // Reverse to newest-first ONCE, in the lib helper, then filter -- never in the markup.
  const recentRows = $derived(tele === null ? [] : applyLegFilter(newestFirst(tele.recent), activeLeg));

  const catalog = $derived(catalogBlock(servingStatus));
  const lastOutcome = $derived(latestCatalogOutcome(servingStatus));
  const addTargets = $derived(catalog === null ? [] : addableProviders(catalog));
  const addProvider = $derived(addTargets.find((p) => p.name === addProviderName) ?? null);
  const addCheck = $derived(catalog === null ? null : addVerdict(addProvider, addId, catalog));
  const catalogNote = $derived(
    statusError !== ""
      ? statusError
      : servingStatus !== null && !servingStatus.available
        ? `Catalog controls are read-only: ${unavailableReasonText(servingStatus.reason)}.`
        : servingStatus !== null && catalog === null
          ? "The dispatcher has not published a catalog projection yet."
          : "",
  );

  async function record(key: string, post: () => Promise<{ requestId: string }>, fallback: string) {
    catalogError = "";
    try {
      const accepted = await post();
      pendingCatalog = { ...pendingCatalog, [key]: accepted.requestId };
    } catch (e) {
      catalogError = reason(e, fallback);
    }
  }

  function toggleCatalogConsumer(provider: ServingCatalogProvider, model: ServingCatalogModel, consumer: ServingConsumer) {
    const key = entryKey(provider.name, model.id);
    if (pendingCatalog[key] !== undefined) return;
    const on = !uiConsumers(model.consumers).includes(consumer);
    const consumers = nextCatalogConsumers(model.consumers, consumer, on);
    void record(key, () => postCatalogConsumers({ provider: provider.name, id: model.id, consumers }), "Unable to record the consumer change");
  }

  function toggleExposureConsumer(modelKey: string, current: readonly string[], consumer: ServingConsumer) {
    const key = entryKey("exposure", modelKey);
    if (pendingCatalog[key] !== undefined) return;
    const on = !uiConsumers(current).includes(consumer);
    const consumers = nextCatalogConsumers(current, consumer, on);
    void record(key, () => postCatalogConsumers({ modelKey, consumers }), "Unable to record the consumer change");
  }

  function removeEntry(provider: ServingCatalogProvider, model: ServingCatalogModel) {
    const key = entryKey(provider.name, model.id);
    if (pendingCatalog[key] !== undefined || !removeVerdict(provider, model).ok) return;
    void record(key, () => postCatalogRemove({ provider: provider.name, id: model.id }), "Unable to record the removal");
  }

  async function submitAdd() {
    if (addProvider === null || addCheck === null || !addCheck.ok) return;
    addBusy = true;
    const provider = addProvider.name;
    const id = addId;
    await record(entryKey(provider, id), () => postCatalogAdd({ provider, id, consumers: addConsumers }), "Unable to record the addition");
    if (catalogError === "") {
      addId = "";
      addConsumers = [];
    }
    addBusy = false;
  }

  function toggleAddConsumer(consumer: ServingConsumer) {
    addConsumers = addConsumers.includes(consumer)
      ? addConsumers.filter((value) => value !== consumer)
      : CATALOG_CONSUMERS.filter((value) => value === consumer || addConsumers.includes(value));
  }
</script>

<p class="eyebrow">Model router</p>
<h1>Router</h1>
<p class="run-lede">
  Read-only status for the inference router, derived entirely from files: the router's own request sink
  (<code>/api/router/telemetry</code>) and the codex sidecar's log (<code>/api/router/sidecar</code>). Each feed
  reports its own availability -- one being off never hides the other. Live model residency is on the Serving
  view. Refresh interval is the header's poll control.
</p>

{#if !loaded && loading}
  <p>Loading…</p>
{:else}
  {#if tele !== null}
    <p class="queue-generated router-ambient">telemetry as of {formatAge(tele.generatedAt, asOf)} ({tele.file})</p>
  {/if}

  <section class="router-panel">
    <div class="router-panel-head">
      <h2>In-flight generations</h2>
      {#if tele !== null}<span class="router-ambient">{tele.inFlight.length} live</span>{/if}
    </div>
    {#if telemetryError}<p class="error-text">{telemetryError}</p>{/if}
    {#if telemetry === null}
      <p class="serving-empty">No telemetry has been read yet.</p>
    {:else if !telemetry.available}
      <p class="serving-empty">Router telemetry unavailable — {unavailableMessage(telemetry.reason)}.</p>
    {:else if telemetry.inFlight.length === 0}
      <p class="serving-empty">No live generations.</p>
    {:else}
      <div class="serving-table-wrap">
        <table class="serving-table">
          <thead>
            <tr>
              <th>Leg</th>
              <th>Model</th>
              <th>Session</th>
              <th>Phase</th>
              <th class="serving-table-num">Elapsed</th>
              <th>Reasoning / response</th>
              <th class="serving-table-num">Rate</th>
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
                <td class="serving-table-num">{elapsedLabel(row.elapsedMs)}</td>
                <td>
                  <div class="router-split" aria-hidden="true">
                    <span class="router-split-reasoning" style="width: {split.reasoningPct}%"></span>
                    <span class="router-split-response" style="width: {split.responsePct}%"></span>
                  </div>
                  <span class="router-ambient">{splitLabel(split, row.reasoningTokens, row.responseTokens)}</span>
                </td>
                <td class="serving-table-num">{tokPerSecLabel(row.tokPerSec)}</td>
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
        <p class="serving-empty">No requests in this window for the selected leg.</p>
      {:else}
        <div class="serving-table-wrap">
          <table class="serving-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Leg</th>
                <th>Model</th>
                <th>Status</th>
                <th>Stop reason</th>
                <th class="serving-table-num">TTFT</th>
                <th class="serving-table-num">Wall</th>
                <th class="serving-table-num">In / out tok</th>
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
                  <td class="serving-table-muted">{row.stopReason ?? "—"}</td>
                  <td class="serving-table-num">{formatDurationMs(row.ttftMs)}</td>
                  <td class="serving-table-num">{formatDurationMs(row.wallMs)}</td>
                  <td class="serving-table-num">{formatCount(row.usage?.input ?? null)} / {formatCount(row.usage?.output ?? null)}</td>
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
      <p class="serving-empty">No telemetry has been read yet.</p>
    {:else}
      <p class="serving-empty">Router telemetry unavailable — {unavailableMessage(telemetry.reason)}.</p>
    {/if}

    <h3>Codex sidecar</h3>
    {#if sidecarError}<p class="error-text">{sidecarError}</p>{/if}
    {#if sidecar === null}
      <p class="serving-empty">No sidecar log has been read yet.</p>
    {:else if !sidecar.available}
      <p class="serving-empty">Sidecar log unavailable — {unavailableMessage(sidecar.reason)}.</p>
    {:else}
      {#if sidecarData !== null}
        <p class="router-ambient">as of {formatAge(sidecarData.generatedAt, asOf)} ({sidecarData.file})</p>
      {/if}
      {#if sidecar.personas.length === 0}
        <p class="serving-empty">No persona activity in this window.</p>
      {:else}
        <div class="serving-table-wrap">
          <table class="serving-table">
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
              <span class="serving-table-muted">{truncateLine(entry.message)}</span>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  </section>

  <section class="router-panel">
    <div class="router-panel-head">
      <h2>Model catalog</h2>
      {#if catalog !== null}<span class="router-ambient">revision <code>{catalog.revision}</code></span>{/if}
      {#if lastOutcome !== null}
        <span class="pill {outcomePillClass(lastOutcome.state)}" title={lastOutcome.requestId}>last change {lastOutcome.state}</span>
      {/if}
    </div>
    {#if lastOutcome !== null && lastOutcome.reason}<p class="serving-table-muted">{lastOutcome.reason}</p>{/if}
    {#if catalogNote}<p class="serving-table-muted">{catalogNote}</p>{/if}
    {#if catalogError}<p class="error-text">{catalogError}</p>{/if}
    {#if catalog !== null}
      <form
        class="control-start-form"
        onsubmit={(event) => {
          event.preventDefault();
          void submitAdd();
        }}
      >
        <div class="field">
          <label for="catalog-add-provider">Provider</label>
          <select id="catalog-add-provider" bind:value={addProviderName}>
            <option value="" disabled>Select a provider</option>
            {#each addTargets as target (target.name)}
              <option value={target.name}>{target.name}</option>
            {/each}
          </select>
        </div>
        <div class="field">
          <label for="catalog-add-id">Model id</label>
          {#if addProvider !== null && addProvider.kind === "local"}
            <select id="catalog-add-id" bind:value={addId}>
              <option value="" disabled>Select an alias</option>
              {#each catalog.addable_local_aliases as alias (alias)}
                <option value={alias}>{alias}</option>
              {/each}
            </select>
          {:else}
            <input id="catalog-add-id" type="text" placeholder="vendor/model or @preset/slug" bind:value={addId} />
          {/if}
        </div>
        <div class="serving-visibility">
          {#each CATALOG_CONSUMERS as consumer (consumer)}
            <button
              type="button"
              class="pill serving-chip {addConsumers.includes(consumer) ? 'pill-ok' : 'pill-muted'}"
              aria-pressed={addConsumers.includes(consumer)}
              onclick={() => toggleAddConsumer(consumer)}
            >
              {consumerLabel(consumer)} {addConsumers.includes(consumer) ? "on" : "off"}
            </button>
          {/each}
        </div>
        <button class="btn-primary" type="submit" disabled={addBusy || addCheck === null || !addCheck.ok} title={addCheck?.reason ?? ""}>
          Add
        </button>
      </form>

      <div class="serving-table-wrap">
        <table class="serving-table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Id</th>
              <th>Consumers</th>
              <th>Remove</th>
            </tr>
          </thead>
          <tbody>
            {#each catalog.providers as provider (provider.name)}
              {#each provider.models as model (entryKey(provider.name, model.id))}
                {@const key = entryKey(provider.name, model.id)}
                {@const pending = pendingCatalog[key] !== undefined}
                {@const removal = removeVerdict(provider, model)}
                <tr>
                  <td>{provider.name}</td>
                  <td>
                    {model.id}
                    {#if model.model_key}<span class="router-ambient">{model.model_key}</span>{/if}
                    {#if isRouterInternal(model.consumers)}<span class="pill pill-tag" title="router-internal id: reachable, never a picker row unless cc is set">router</span>{/if}
                  </td>
                  <td>
                    <div class="serving-visibility">
                      {#each CATALOG_CONSUMERS as consumer (consumer)}
                        {@const on = uiConsumers(model.consumers).includes(consumer)}
                        {@const verdict = chipVerdict(consumer, true)}
                        <button
                          type="button"
                          class="pill serving-chip {pending ? 'pill-info' : on ? 'pill-ok' : 'pill-muted'}"
                          disabled={pending || !verdict.ok}
                          aria-pressed={on}
                          title={pending ? "requested — waiting for the dispatcher" : (verdict.reason ?? "")}
                          onclick={() => toggleCatalogConsumer(provider, model, consumer)}
                        >
                          {consumerLabel(consumer)} {pending ? "requested" : on ? "on" : "off"}
                        </button>
                      {/each}
                    </div>
                  </td>
                  <td>
                    <button
                      type="button"
                      class="btn-secondary"
                      disabled={pending || !removal.ok}
                      title={removal.reason ?? ""}
                      onclick={() => removeEntry(provider, model)}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              {/each}
            {/each}
            {#each catalog.exposures as exposure (exposure.model_key)}
              {@const key = entryKey("exposure", exposure.model_key)}
              {@const pending = pendingCatalog[key] !== undefined}
              <tr>
                <td class="serving-table-muted">exposure</td>
                <td>{exposure.model_key}</td>
                <td>
                  <div class="serving-visibility">
                    {#each CATALOG_CONSUMERS as consumer (consumer)}
                      {@const on = exposure.consumers.includes(consumer)}
                      {@const verdict = chipVerdict(consumer, false)}
                      <button
                        type="button"
                        class="pill serving-chip {pending ? 'pill-info' : on ? 'pill-ok' : 'pill-muted'}"
                        disabled={pending || !verdict.ok}
                        aria-pressed={on}
                        title={pending ? "requested — waiting for the dispatcher" : (verdict.reason ?? "")}
                        onclick={() => toggleExposureConsumer(exposure.model_key, exposure.consumers, consumer)}
                      >
                        {consumerLabel(consumer)} {pending ? "requested" : on ? "on" : "off"}
                      </button>
                    {/each}
                  </div>
                </td>
                <td class="serving-table-muted">served by llama-swap</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>
{/if}
