<script lang="ts">
  // Queue -- read-only visibility over `llama/bakeoff/queues/*.json` joined against their
  // sibling `<stem>.state.json` by alias (plan WP-C, `bench-dashboard-run-context-
  // surfacing.md`), PLUS the control panel (WP-5, `bakeoff-queue-control-plane.md`) --
  // arm/disarm, start, stop, and per-request cancel, all against `control`, the read-only
  // projection `GET /api/queue` (WP-4) now carries. The queue/cell table itself stays
  // exactly as read-only as before (no mutating route touches queue/state files directly --
  // every `POST /api/control/*` only ever RECORDS a request into the host dispatcher's
  // drop-box; "accepted" means recorded, never executed). Independent fetch keyed on
  // `refreshToken`, same shape Serving.svelte's own `loadServing` effect uses; a successful
  // control POST also triggers an immediate `loadQueue()` re-fetch (Accepted != executed --
  // the UI learns real outcomes only from `control`, never from the POST response alone).
  import type { ControlSection, QueueEntry, QueueErrorEntry, QueueListItem } from "@bench-dashboard/shared";
  import { getQueue, postControlArm, postControlCancel, postControlDisarm, postControlStart, postControlStop } from "../api.ts";
  import { cellStateLabel, cellStatePillClass, queueStateLabel, queueStatePillClass } from "../lib/queue/status.ts";
  import { formatNextWindowEstimate, formatQueueEtaLine } from "../lib/queue/eta.ts";
  import {
    armedLabel,
    armedPillClass,
    controlRequestStateLabel,
    controlRequestStatePillClass,
    lastRunDispositionLabel,
    lastRunDispositionPillClass,
    START_MINUTES_MAX,
    START_MINUTES_MIN,
    validateStartForm,
  } from "../lib/queue/control.ts";
  import { vramProfileLabel, vramProfilePillClass } from "../lib/profile.ts";
  import { formatCount, formatTimestamp } from "../lib/format.ts";

  let { refreshToken }: { refreshToken: number } = $props();

  let queues = $state<QueueListItem[]>([]);
  let generatedAt = $state<string | null>(null);
  let queueLoading = $state(false);
  let queueError = $state("");
  let control = $state<ControlSection | null>(null);

  // Control-panel action state -- separate from `queueError`/`queueLoading` (a fetch
  // failure and an action failure are distinct situations; both can be true at once).
  let feedback = $state<{ requestId: string; message: string | undefined } | null>(null);
  let actionError = $state("");
  let armBusy = $state(false);
  let startBusy = $state(false);
  let stopBusy = $state(false);
  let cancelBusyId = $state<string | null>(null);
  let startStem = $state("");
  let startMinutes = $state(60);
  let startFormError = $state("");

  async function loadQueue() {
    queueLoading = true;
    queueError = "";
    try {
      const response = await getQueue();
      queues = response.queues;
      generatedAt = response.generatedAt;
      control = response.control ?? null;
    } catch (e) {
      queueError = e instanceof Error ? e.message : "Unable to load the queue";
    } finally {
      queueLoading = false;
    }
  }

  $effect(() => {
    // eslint-disable-next-line no-unused-expressions -- bare read registers the $effect dependency
    refreshToken;
    void loadQueue();
  });

  function recordFeedback(response: { requestId: string; message?: string }) {
    feedback = { requestId: response.requestId, message: response.message };
    actionError = "";
  }

  async function handleArmToggle() {
    if (control === null) return;
    armBusy = true;
    try {
      const response = control.armed ? await postControlDisarm() : await postControlArm();
      recordFeedback(response);
      await loadQueue();
    } catch (e) {
      actionError = e instanceof Error ? e.message : "Unable to record the arm/disarm request";
    } finally {
      armBusy = false;
    }
  }

  async function handleStart() {
    const validation = validateStartForm({ queueStem: startStem, minutes: startMinutes });
    if (!validation.valid) {
      startFormError = validation.error ?? "Invalid start request.";
      return;
    }
    startFormError = "";
    startBusy = true;
    try {
      const response = await postControlStart({ queueStem: startStem, minutes: startMinutes });
      recordFeedback(response);
      await loadQueue();
    } catch (e) {
      actionError = e instanceof Error ? e.message : "Unable to record the start request";
    } finally {
      startBusy = false;
    }
  }

  async function handleStop() {
    stopBusy = true;
    try {
      const response = await postControlStop();
      recordFeedback(response);
      await loadQueue();
    } catch (e) {
      actionError = e instanceof Error ? e.message : "Unable to record the stop request";
    } finally {
      stopBusy = false;
    }
  }

  async function handleCancel(targetRequestId: string) {
    cancelBusyId = targetRequestId;
    try {
      const response = await postControlCancel({ targetRequestId });
      recordFeedback(response);
      await loadQueue();
    } catch (e) {
      actionError = e instanceof Error ? e.message : "Unable to record the cancel request";
    } finally {
      cancelBusyId = null;
    }
  }

  // `QueueListItem` is a zod union (`QueueEntrySchema | QueueErrorEntrySchema`), not a
  // discriminated one -- an `error` key only ever appears on the error shape.
  function isQueueError(item: QueueListItem): item is QueueErrorEntry {
    return "error" in item;
  }

  // Start-form stem options: only queues that read cleanly (an error entry has no `name`
  // that's safe to hand the dispatcher).
  const startableQueueNames = $derived(queues.filter((item) => !isQueueError(item)).map((item) => item.name));

  const CELL_COUNT_ORDER: (keyof QueueEntry["cellCounts"])[] = [
    "pending",
    "running",
    "completed",
    "failed",
    "skipped-propagated",
    "aborted-hang",
  ];
</script>

<p class="eyebrow">Bake-off queues</p>
<h1>Queue</h1>
<p class="run-lede">
  Read-only status from <code>llama/bakeoff/queues/*.json</code>, joined against each queue's live/terminal
  <code>*.state.json</code> by alias. Cells with no matching state entry render unjoined -- they haven't run under
  a state file that stamps aliases, not a failure.
</p>
{#if queueLoading}
  <p>Loading…</p>
{:else if queueError}
  <p class="error-text">{queueError}</p>
{:else}
  {#if generatedAt}<p class="queue-generated">as of {formatTimestamp(generatedAt)}</p>{/if}

  {#if control === null}
    <p class="run-note">
      The control plane is not configured for this deployment -- arming, starting, stopping, or canceling a queue
      isn't available here. The queue/cell status below is unaffected.
    </p>
  {:else}
    <div class="control-panel">
      <h2>Control</h2>
      <p class="queue-generated">control status as of {formatTimestamp(control.statusGeneratedAt)}</p>

      {#if control.stale}
        <p class="run-note control-stale-banner">
          <span class="pill pill-warn">stale</span>
          The dispatcher hasn't published control status in over 180 seconds. Actions submitted below are still recorded
          and will run once the dispatcher returns.
        </p>
      {/if}

      <div class="control-row">
        <span>Nightly window:</span>
        <span class="pill {armedPillClass(control.armed)}">{armedLabel(control.armed)}</span>
        <button class="btn-secondary" disabled={armBusy} onclick={() => void handleArmToggle()}>
          {control.armed ? "Disarm" : "Arm"}
        </button>
      </div>

      <form
        class="control-start-form"
        onsubmit={(event) => {
          event.preventDefault();
          void handleStart();
        }}
      >
        <div class="field">
          <label for="control-start-stem">Queue</label>
          <select id="control-start-stem" bind:value={startStem} disabled={startableQueueNames.length === 0}>
            <option value="" disabled>Select a queue</option>
            {#each startableQueueNames as name (name)}
              <option value={name}>{name}</option>
            {/each}
          </select>
        </div>
        <div class="field">
          <label for="control-start-minutes">Minutes</label>
          <input
            id="control-start-minutes"
            type="number"
            min={START_MINUTES_MIN}
            max={START_MINUTES_MAX}
            bind:value={startMinutes}
          />
        </div>
        <button class="btn-primary" type="submit" disabled={startBusy || startableQueueNames.length === 0}>Start</button>
      </form>
      {#if startFormError}<p class="error-text">{startFormError}</p>{/if}

      <button class="btn-danger" disabled={stopBusy} onclick={() => void handleStop()}>Stop</button>

      {#if feedback}
        <p class="control-feedback">Recorded as <code>{feedback.requestId}</code>.</p>
        {#if feedback.message}<p class="control-feedback-message">{feedback.message}</p>{/if}
      {/if}
      {#if actionError}<p class="error-text">{actionError}</p>{/if}

      <section class="run-meta-grid">
        {#if control.live}
          <div class="field">
            <span>Live request</span>
            <code>{control.live.requestId}</code>
          </div>
          <div class="field">
            <span>Live queue</span>
            <code>{control.live.queueStem}</code>
          </div>
          <div class="field">
            <span>Phase</span>
            <span>{control.live.phase}</span>
          </div>
          <div class="field">
            <span>Started</span>
            <span>{formatTimestamp(control.live.startedAt)}</span>
          </div>
        {:else}
          <div class="field">
            <span>Live</span>
            <span>No run is currently live.</span>
          </div>
        {/if}

        {#if control.lastRun}
          <div class="field">
            <span>Last run</span>
            <code>{control.lastRun.requestId}</code>
          </div>
          <div class="field">
            <span>Ended</span>
            <span>{formatTimestamp(control.lastRun.endedAt)}</span>
          </div>
          <div class="field">
            <span>Run / verify exit</span>
            <span>{formatCount(control.lastRun.runExit)} / {formatCount(control.lastRun.verifyExit)}</span>
          </div>
          <div class="field">
            <span>Disposition</span>
            <span class="pill {lastRunDispositionPillClass(control.lastRun.disposition)}">
              {lastRunDispositionLabel(control.lastRun.disposition)}
            </span>
          </div>
          {#if control.lastRun.restartFailed !== null}
            <div class="field">
              <span>Router restart</span>
              <span class="pill pill-error" title={control.lastRun.restartFailed}>failed — GPU tier is down until started by hand</span>
            </div>
          {/if}
        {/if}
      </section>

      {#if control.anomalies.length > 0}
        <div class="control-anomalies">
          <h3>Anomalies</h3>
          <ul>
            {#each control.anomalies as anomaly, index (index)}
              <li class="control-anomaly"><span class="pill pill-warn">anomaly</span> {anomaly}</li>
            {/each}
          </ul>
        </div>
      {/if}

      <h3>Request history</h3>
      {#if control.requests.length === 0}
        <p class="run-note">No control requests recorded yet.</p>
      {:else}
        <div class="run-table-wrap">
          <table class="run-table">
            <thead>
              <tr>
                <th>Request</th>
                <th>Action</th>
                <th>State</th>
                <th>Reason</th>
                <th>Requested</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {#each control.requests as row (row.requestId)}
                <tr>
                  <td><code>{row.requestId}</code></td>
                  <td>{row.action}</td>
                  <td>
                    <span class="pill {controlRequestStatePillClass(row.state)}">{controlRequestStateLabel(row.state)}</span>
                  </td>
                  <td class="run-table-muted">{row.reason ?? "—"}</td>
                  <td>{formatTimestamp(row.requestedAt)}</td>
                  <td>
                    {#if row.state === "pending"}
                      <button
                        class="btn-secondary"
                        disabled={cancelBusyId === row.requestId}
                        onclick={() => void handleCancel(row.requestId)}
                      >
                        Cancel
                      </button>
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </div>
  {/if}

  {#if queues.length === 0}
    <p class="run-empty">
      No queues configured -- either <code>QUEUES_DIR</code> is unset, or <code>llama/bakeoff/queues/</code> has no
      queue files.
    </p>
  {:else}
  <div class="queue-list">
    {#each queues as item (item.name)}
      {#if isQueueError(item)}
        <div class="queue-card">
          <div class="queue-card-header">
            <span class="queue-card-name">{item.name}</span>
            <span class="pill pill-error">error</span>
          </div>
          <p class="error-text">{item.error}</p>
        </div>
      {:else}
        {@const nextWindowEstimate = formatNextWindowEstimate(item.eta)}
        <div class="queue-card">
          <div class="queue-card-header">
            <span class="queue-card-name">{item.name}</span>
            <span class="pill {queueStatePillClass(item.queueState)}">{queueStateLabel(item.queueState)}</span>
            <span class="pill {vramProfilePillClass(item.vramProfile)}">{vramProfileLabel(item.vramProfile)}</span>
          </div>
          <div class="queue-card-meta">
            <span>Deadline: {item.deadline ?? "—"}</span>
            <span class="queue-eta-line">{formatQueueEtaLine(item.eta)}</span>
            {#if nextWindowEstimate !== null}
              <span class="queue-eta-line">{nextWindowEstimate}</span>
            {/if}
          </div>
          <div class="queue-counts">
            {#each CELL_COUNT_ORDER as state (state)}
              <span class="pill {cellStatePillClass(item.cellCounts[state] === null ? null : state)}">
                {state}: {formatCount(item.cellCounts[state])}
              </span>
            {/each}
          </div>

          {#if item.cells.length === 0}
            <p class="run-empty">This queue file has no cells.</p>
          {:else}
            <div class="run-table-wrap">
              <table class="run-table">
                <thead>
                  <tr>
                    <th>Alias</th>
                    <th>Model</th>
                    <th class="run-table-num">c</th>
                    <th>ctk</th>
                    <th>ctv</th>
                    <th>fa</th>
                    <th>Sampling arm</th>
                    <th class="run-table-num">Trials</th>
                    <th>State</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {#each item.cells as cell (cell.alias)}
                    <tr>
                      <td><code>{cell.alias}</code></td>
                      <td>{cell.model ?? "—"}</td>
                      <td class="run-table-num">{formatCount(cell.c)}</td>
                      <td>{cell.ctk ?? "—"}</td>
                      <td>{cell.ctv ?? "—"}</td>
                      <td>{cell.fa ?? "—"}</td>
                      <td>{cell.samplingArm ?? "—"}</td>
                      <td class="run-table-num">{formatCount(cell.trials)}</td>
                      <td><span class="pill {cellStatePillClass(cell.state)}">{cellStateLabel(cell.state)}</span></td>
                      <td class="run-table-muted">{cell.reason ?? "—"}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </div>
      {/if}
    {/each}
  </div>
  {/if}
{/if}
