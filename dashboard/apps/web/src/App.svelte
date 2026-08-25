<script lang="ts">
  import { Activity } from "lucide-svelte";
  import { AppHeader, PollControl } from "@fleet-tools/signalworks";
  import type { ExperimentSummary } from "@bench-dashboard/shared";
  import { getExperiments } from "./api.ts";
  import { guardedNavClick, parsePath, runDetailPath, tabToPath, type Tab } from "./routes.ts";
  import Runs from "./views/Runs.svelte";
  import Ladders from "./views/Ladders.svelte";
  import LongContext from "./views/LongContext.svelte";
  import Fit from "./views/Fit.svelte";
  import Serving from "./views/Serving.svelte";
  import Queue from "./views/Queue.svelte";

  // WP-2: the five real views (Runs/Ladders/Long-context/Fit/Serving) on top of the full
  // API WP-1 already ships. `experiments` is fetched ONCE here (App owns it, same shape as
  // model_scoreboard's App.svelte owning `catalog`) and passed down -- every view that needs
  // more (a ladder, a curve, a detail, the serving join) fetches that itself, keyed on
  // `refreshToken` so a poll tick or manual refresh re-pulls it too. WP-C adds a sixth,
  // read-only Queue view over `GET /api/queue` -- it needs no `experiments` join, only
  // `refreshToken` (same independent-fetch shape as Serving).

  const NAV_TABS: { tab: Tab; label: string }[] = [
    { tab: "runs", label: "Runs" },
    { tab: "ladders", label: "Ladders" },
    { tab: "longcontext", label: "Long-context" },
    { tab: "fit", label: "Fit" },
    { tab: "serving", label: "Serving" },
    { tab: "queue", label: "Queue" },
  ];

  // Serving is the default landing tab (WP-2, `plans/capability-surfacing-and-local-agent-
  // endpoints.md`: "Serving becomes the default landing route of the SPA", `routes.ts`
  // `TAB_PATHS.serving === "/"`) -- `applyLocation()` below overwrites this from the real
  // URL before first render, so this initial value only matters as the pre-navigation
  // fallback, same as `routes.ts`'s own `UNMATCHED` default.
  let activeTab = $state<Tab>("serving");
  // Only meaningful while activeTab === "runs" (routes.ts `/runs/:id`): Runs renders a
  // drill-down page when this is set, its list otherwise.
  let activeRunId = $state<string | undefined>(undefined);

  let experiments = $state<ExperimentSummary[]>([]);
  let loading = $state(true);
  let error = $state("");
  // Bumped on every successful refresh -- the one-way signal every view's own independent
  // fetch (ladders/curves/serving/fit-detail) re-pulls on, same `refreshToken` shape
  // `model_scoreboard`'s Analysis/Feed views use against their own PollControl.
  let refreshToken = $state(0);

  async function refresh() {
    error = "";
    try {
      experiments = await getExperiments();
      refreshToken += 1;
    } catch (e) {
      error = e instanceof Error ? e.message : "Unable to reach the dashboard API";
    } finally {
      loading = false;
    }
  }

  function goToTab(tab: Tab) {
    activeTab = tab;
    if (tab === "runs") activeRunId = undefined;
    history.pushState(null, "", tabToPath(tab));
  }

  function goToRun(id: string) {
    activeRunId = id;
    activeTab = "runs";
    history.pushState(null, "", runDetailPath(id));
  }

  function backToRuns() {
    activeRunId = undefined;
    history.pushState(null, "", tabToPath("runs"));
  }

  function applyLocation() {
    const route = parsePath(location.pathname);
    activeTab = route.tab;
    activeRunId = route.tab === "runs" ? route.runId : undefined;
    if (!route.matched) history.replaceState(null, "", "/");
  }

  applyLocation();
  refresh();
</script>

<svelte:window onpopstate={applyLocation} />

<AppHeader tool="Bench Dashboard" icon={Activity} appId="bench-dashboard">
  <nav class="sw-nav" slot="nav" aria-label="Bench Dashboard views">
    {#each NAV_TABS as { tab, label } (tab)}
      <a
        href={tabToPath(tab)}
        class:active={activeTab === tab}
        aria-current={activeTab === tab ? "page" : undefined}
        onclick={(event) => guardedNavClick(event, () => goToTab(tab))}
      >
        {label}
      </a>
    {/each}
  </nav>
  <PollControl slot="poll" storageKey="bench-dashboard.poll.v1" defaultMs={0} busy={loading} onTick={refresh} />
</AppHeader>

<main class="page wide">
  {#if activeTab === "runs"}
    <Runs {experiments} {loading} {error} runId={activeRunId} {refreshToken} onopenrun={goToRun} onback={backToRuns} />
  {:else if activeTab === "ladders"}
    <Ladders {experiments} {loading} {error} {refreshToken} />
  {:else if activeTab === "longcontext"}
    <LongContext {experiments} {loading} {error} {refreshToken} />
  {:else if activeTab === "fit"}
    <Fit {experiments} {loading} {error} {refreshToken} />
  {:else if activeTab === "serving"}
    <Serving {experiments} {loading} {error} {refreshToken} onopenrun={goToRun} />
  {:else}
    <Queue {refreshToken} />
  {/if}
</main>
