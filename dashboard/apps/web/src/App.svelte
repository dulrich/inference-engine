<script lang="ts">
  import { Activity } from "lucide-svelte";
  import { AppHeader, PollControl } from "@fleet-tools/signalworks";
  import { guardedNavClick, NAV_GROUPS, parsePath, tabArea, tabToPath, type Tab } from "./routes.ts";
  import Serving from "./views/Serving.svelte";
  import Router from "./views/Router.svelte";

  // The serving dashboard: two views, Serving (the default landing route, "/") and Router.
  // The research views moved to the research dashboard (eval-harness `inference_research`,
  // :4816; `research-to-eval-harness` WP-4). Each view self-fetches its own feeds keyed on
  // `refreshToken`; the single PollControl below is the only refresh clock in the app
  // (CC-06/07), and the token no longer depends on any one fetch succeeding.
  let activeTab = $state<Tab>("serving");

  // Bumped on every poll tick or manual refresh -- the one-way signal every view's own
  // independent fetch re-pulls on.
  let refreshToken = $state(0);

  function refresh() {
    refreshToken += 1;
  }

  function goToTab(tab: Tab) {
    activeTab = tab;
    history.pushState(null, "", tabToPath(tab));
  }

  function applyLocation() {
    const route = parsePath(location.pathname);
    activeTab = route.tab;
    if (!route.matched) history.replaceState(null, "", "/");
  }

  applyLocation();
</script>

<svelte:window onpopstate={applyLocation} />

<AppHeader tool="Bench Dashboard" icon={Activity} appId="bench-dashboard">
  <nav class="sw-nav" slot="nav" aria-label="Bench Dashboard views">
    {#each NAV_GROUPS as group (group.area)}
      <div class="nav-group" class:nav-group-active={tabArea(activeTab) === group.area} role="group" aria-label={group.label}>
        {#if group.tabs.length > 1}
          <span class="nav-group-label">{group.label}</span>
        {/if}
        {#each group.tabs as { tab, label } (tab)}
          <a
            href={tabToPath(tab)}
            class:active={activeTab === tab}
            aria-current={activeTab === tab ? "page" : undefined}
            onclick={(event) => guardedNavClick(event, () => goToTab(tab))}
          >
            {label}
          </a>
        {/each}
      </div>
    {/each}
  </nav>
  <PollControl slot="poll" storageKey="bench-dashboard.poll.v1" defaultMs={0} busy={false} onTick={refresh} />
</AppHeader>

<main class="page wide">
  {#if activeTab === "serving"}
    <Serving {refreshToken} />
  {:else}
    <Router {refreshToken} />
  {/if}
</main>
