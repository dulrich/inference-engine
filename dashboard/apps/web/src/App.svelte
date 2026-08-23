<script lang="ts">
  import { Activity } from "lucide-svelte";
  import { AppHeader } from "@fleet-tools/signalworks";
  import { getExperiments, getHealth } from "./api.ts";
  import type { ExperimentSummary, HealthResponse } from "@bench-dashboard/shared";

  // WP-1 stub (brief's File scope pin): proves API wiring with a health + experiment-count
  // readout. WP-2 replaces this body with the real views (Runs/Ladders/Long-context/Fit/
  // Serving) on top of the API this WP already ships in full.

  let health = $state<HealthResponse | null>(null);
  let experiments = $state<ExperimentSummary[]>([]);
  let error = $state("");
  let loading = $state(true);

  async function refresh() {
    error = "";
    try {
      const [nextHealth, nextExperiments] = await Promise.all([getHealth(), getExperiments()]);
      health = nextHealth;
      experiments = nextExperiments;
    } catch (e) {
      error = e instanceof Error ? e.message : "Unable to reach the dashboard API";
    } finally {
      loading = false;
    }
  }

  refresh();
</script>

<AppHeader tool="Bench Dashboard" icon={Activity} appId="bench-dashboard" />

<main class="page">
  <h1>Bench Dashboard</h1>
  <p class="lede">
    Bake-off measurement records (<code>runs/bakeoff/</code>), served over
    <code>127.0.0.1:4810</code>. Views land in WP-2 — this is the WP-1 wiring stub.
  </p>

  {#if loading}
    <p>Loading…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else}
    <div class="stat-row">
      <div class="stat">
        <span class="stat-label">API health</span>
        <span class="stat-value">{health?.status ?? "unknown"}</span>
      </div>
      <div class="stat">
        <span class="stat-label">Ingested experiments</span>
        <span class="stat-value">{experiments.length}</span>
      </div>
    </div>
  {/if}
</main>

<style>
  .lede {
    color: var(--text-muted, var(--text));
    max-width: 60ch;
  }
  .stat-row {
    display: flex;
    gap: 24px;
    margin-top: 16px;
  }
  .stat {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .stat-label {
    font-size: 0.8em;
    opacity: 0.75;
  }
  .stat-value {
    font-size: 1.6em;
    font-weight: 600;
  }
  .error {
    color: var(--error-text, var(--error));
  }
</style>
