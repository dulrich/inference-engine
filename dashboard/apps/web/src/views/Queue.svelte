<script lang="ts">
  // Queue -- read-only visibility over `llama/bakeoff/queues/*.json` joined against their
  // sibling `<stem>.state.json` by alias (plan WP-C, `bench-dashboard-run-context-
  // surfacing.md`). `GET /api/queue` (WP-A) does all the reading/joining server-side; this
  // view only renders what it returns -- no mutating route exists here, and none is added
  // by this plan (Decisions locked: "Read-only throughout"). Independent fetch keyed on
  // `refreshToken`, same shape Serving.svelte's own `loadServing` effect uses.
  import type { QueueEntry, QueueErrorEntry, QueueListItem } from "@bench-dashboard/shared";
  import { getQueue } from "../api.ts";
  import { cellStateLabel, cellStatePillClass, queueStateLabel, queueStatePillClass } from "../lib/queue/status.ts";
  import { vramProfileLabel, vramProfilePillClass } from "../lib/profile.ts";
  import { formatCount, formatTimestamp } from "../lib/format.ts";

  let { refreshToken }: { refreshToken: number } = $props();

  let queues = $state<QueueListItem[]>([]);
  let generatedAt = $state<string | null>(null);
  let queueLoading = $state(false);
  let queueError = $state("");

  async function loadQueue() {
    queueLoading = true;
    queueError = "";
    try {
      const response = await getQueue();
      queues = response.queues;
      generatedAt = response.generatedAt;
    } catch (e) {
      queueError = e instanceof Error ? e.message : "Unable to load the queue";
    } finally {
      queueLoading = false;
    }
  }

  $effect(() => {
    refreshToken;
    void loadQueue();
  });

  // `QueueListItem` is a zod union (`QueueEntrySchema | QueueErrorEntrySchema`), not a
  // discriminated one -- an `error` key only ever appears on the error shape.
  function isQueueError(item: QueueListItem): item is QueueErrorEntry {
    return "error" in item;
  }

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
<p class="run-note">
  Starting, stopping, or otherwise arming a queue is not done here -- this tab is read-only. Manual queue control is
  planned future work (the <code>bakeoff-queue-control-plane</code> successor), not built yet.
</p>

{#if queueLoading}
  <p>Loading…</p>
{:else if queueError}
  <p class="error-text">{queueError}</p>
{:else if queues.length === 0}
  <p class="run-empty">
    No queues configured -- either <code>QUEUES_DIR</code> is unset, or <code>llama/bakeoff/queues/</code> has no
    queue files.
  </p>
{:else}
  {#if generatedAt}<p class="queue-generated">as of {formatTimestamp(generatedAt)}</p>{/if}
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
        <div class="queue-card">
          <div class="queue-card-header">
            <span class="queue-card-name">{item.name}</span>
            <span class="pill {queueStatePillClass(item.queueState)}">{queueStateLabel(item.queueState)}</span>
            <span class="pill {vramProfilePillClass(item.vramProfile)}">{vramProfileLabel(item.vramProfile)}</span>
          </div>
          <div class="queue-card-meta">
            <span>Deadline: {item.deadline ?? "—"}</span>
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
