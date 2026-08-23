<script lang="ts">
  // The fleet's single refresh affordance (CC-19): one fused group — interval
  // select · countdown (auto only) · RefreshCw segment. Never a second text
  // "Refresh" button. Full contract: references/polling-refresh-standard.md.
  import { RefreshCw } from "lucide-svelte";
  import { onDestroy, onMount } from "svelte";
  import { createPoller, DEFAULT_POLL_OPTIONS, type Poller, type PollOption } from "./createPoller";
  import { createLiveSource, type LiveSource, type LiveState } from "./createLiveSource";

  /** localStorage key, convention `<app>.<feature>.v1`. */
  export let storageKey: string;
  /**
   * Called on every poll tick (interval elapsed, tab regained visibility, or
   * unpaused while polling). Keep this the *light* refresh — it runs unattended.
   */
  export let onTick: () => void | Promise<void>;
  /**
   * Interval used when nothing valid is stored. Required: every surface makes an
   * explicit call on whether it auto-polls by default.
   */
  export let defaultMs: PollOption;
  /**
   * Called when the user clicks the refresh segment. Defaults to `onTick`. Pass the
   * page's *full* reload here when a tick only refreshes part of the view: this
   * control is the page's single refresh affordance.
   */
  export let onRefresh: (() => void | Promise<void>) | undefined = undefined;
  /** Validated interval options in ms; 0 = manual. */
  export let options: readonly PollOption[] = DEFAULT_POLL_OPTIONS;
  /**
   * App-level pause for when the polled view isn't on screen (a non-active
   * route/tab). Freezes the countdown but keeps the interval — pause ≠ Manual.
   */
  export let paused = false;
  /** Disables the refresh segment while a load is already in flight. */
  export let busy = false;
  /**
   * Notified with the validated interval on mount and on every change. Use when the
   * parent must share the interval; the poller itself stays owned by this component.
   */
  export let onIntervalChange: ((ms: PollOption) => void) | undefined = undefined;
  /**
   * Opt-in live (SSE) mode. When given, the select gains a **Live** option that switches
   * refreshes onto the stream at `url` and suspends the interval timer; the user's
   * numeric interval is kept as the fallback and resumes automatically when the stream
   * degrades. Omit the prop and nothing changes: no Live option, no EventSource.
   * Endpoint contract (named `version`/`heartbeat` events): see `createLiveSource.ts`.
   */
  export let liveSource: { url: string } | undefined = undefined;

  /** Select value standing in for "not an interval — the live stream". */
  const LIVE_VALUE = "live";

  let intervalMs: PollOption = defaultMs;
  let poller: Poller | undefined;
  let live: LiveSource | undefined;
  let liveEnabled = false;
  let liveState: LiveState = "off";
  let remainingMs: number | null = null;
  let ticker: ReturnType<typeof setInterval> | undefined;
  let sweeping = false;
  let sweepKey = 0;

  // Live holds the timer down while it owns refreshes — including the pre-grace
  // `connecting` window, so a brief reconnect doesn't flip the surface back to polling.
  // Only `degraded` (and Live being off) hands the duty back to the numeric poller.
  $: liveHolds = liveEnabled && (liveState === "live" || liveState === "connecting");
  $: selectValue = liveEnabled ? LIVE_VALUE : intervalMs;
  $: liveLabel =
    liveState === "live"
      ? "Live updates: connected"
      : liveState === "degraded"
        ? "Live updates: unavailable — polling"
        : "Live updates: connecting";

  function optionLabel(ms: PollOption): string {
    if (ms === 0) return "Manual";
    if (ms < 60_000) return `Auto ${ms / 1_000}s`;
    return `Auto ${ms / 60_000}m`;
  }

  // Countdown: minute-granular above 60s, seconds at 60-to-0.
  function countdownLabel(ms: number): string {
    if (ms > 60_000) return `${Math.ceil(ms / 60_000)}m`;
    return `${Math.max(0, Math.ceil(ms / 1_000))}s`;
  }

  /** One-shot accent sweep, once per poll firing — auto and click, never continuous. */
  function sweep() {
    sweepKey += 1;
    sweeping = true;
  }

  function firedTick() {
    sweep();
    return onTick();
  }

  function updateRemaining() {
    if (paused) return; // frozen — keeps the pre-pause value on screen
    const at = poller?.nextTickAt();
    remainingMs = at === undefined ? null : Math.max(0, at - Date.now());
  }

  function handleChange(event: Event) {
    const raw = (event.currentTarget as HTMLSelectElement).value;
    if (raw === LIVE_VALUE) {
      // Selecting Live never writes the interval key: the stored interval stays the
      // user's fallback preference, so `onIntervalChange` has nothing new to report.
      live?.setEnabled(true);
      liveEnabled = live?.isEnabled() ?? false;
      updateRemaining();
      return;
    }
    if (liveEnabled) {
      live?.setEnabled(false);
      liveEnabled = live?.isEnabled() ?? false;
    }
    const next = Number(raw);
    poller?.setIntervalMs(next);
    intervalMs = poller?.getIntervalMs() ?? next;
    onIntervalChange?.(intervalMs);
    updateRemaining();
  }

  // Click = refresh + restart(): polls immediately and, when auto is on, resets
  // the interval so the next auto poll lands one full interval from now.
  function refreshNow() {
    sweep();
    void (onRefresh ?? onTick)();
    poller?.restart();
    updateRemaining();
  }

  onMount(() => {
    poller = createPoller({ storageKey, options, onTick: firedTick, defaultMs, paused });
    intervalMs = poller.getIntervalMs();
    onIntervalChange?.(intervalMs);
    if (liveSource) {
      live = createLiveSource({
        storageKey,
        url: liveSource.url,
        onTick: firedTick,
        onStateChange: (next) => (liveState = next),
        paused,
      });
      liveEnabled = live.isEnabled();
      liveState = live.getState();
    }
    updateRemaining();
    ticker = setInterval(updateRemaining, 250);
  });

  // setPaused() no-ops on an unchanged value, so this is safe to run on mount (and when
  // `poller` is first assigned) without double-firing the unpause tick. `liveHolds` folds
  // the live mode in: while the stream owns refreshes the timer stays down, and losing the
  // stream unpauses it — which fires the same catch-up tick an unpause always does.
  $: poller?.setPaused(paused || liveHolds);
  $: live?.setPaused(paused);

  onDestroy(() => {
    if (ticker) clearInterval(ticker);
    poller?.destroy();
    live?.destroy();
  });
</script>

<span class="poll-group" role="group" aria-label="Auto-refresh">
  <select value={selectValue} on:change={handleChange} aria-label="Refresh interval">
    {#each options as option (option)}
      <option value={option}>{optionLabel(option)}</option>
    {/each}
    {#if liveSource}
      <option value={LIVE_VALUE}>Live</option>
    {/if}
  </select>
  {#if liveEnabled}
    <!-- Live mode reuses the countdown square: there is no countdown to show, so the
         segment carries the connection state instead (CC-19). -->
    <span
      class="countdown conn"
      class:is-live={liveState === "live"}
      class:is-degraded={liveState === "degraded"}
      role="status"
      aria-label={liveLabel}
      title={liveLabel}
    >
      <span class="dot" aria-hidden="true"></span>
    </span>
  {:else if intervalMs > 0 && remainingMs !== null}
    <span class="countdown" class:frozen={paused} aria-hidden="true">{countdownLabel(remainingMs)}</span>
  {/if}
  <button
    type="button"
    class="refresh"
    class:auto={intervalMs > 0}
    on:click={refreshNow}
    disabled={busy}
    aria-label="Refresh now"
    title="Refresh now"
  >
    {#key sweepKey}
      {#if sweeping}
        <span class="sweep" on:animationend={() => (sweeping = false)}></span>
      {/if}
    {/key}
    <span class="glyph"><RefreshCw size={13} aria-hidden="true" /></span>
  </button>
</span>

<style>
  .poll-group {
    display: inline-flex;
    align-items: stretch;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: var(--r-control);
    background: var(--panel);
  }

  /* Segments are flush with square inner corners; only the container rounds.
     Shared 32px control height (CC-06/07/19): the strip lands on the same
     34px line (32px control + 1px frame) as SurroundControl and the home link. */
  /* Geometry is declared exhaustively (incl. width and min-*) so consumer-side
     bare element selectors (e.g. `select { width: 100% }`, `button {
     border-radius; min-height }`) can never fill an undeclared property —
     scoped rules only win where they actually declare. */
  select {
    width: auto;
    min-width: 0;
    max-width: none;
    min-height: 32px;
    height: 32px;
    padding: 0 10px;
    border: 0;
    border-radius: 0;
    background: var(--inset);
    color: var(--text);
    font: inherit;
    font-size: 0.76rem;
  }

  /* Countdown and refresh segments are exact 32×32 squares — flex: none so a
     tight header rail squeezes the select, never the squares. */
  .countdown {
    display: grid;
    place-items: center;
    flex: none;
    width: 32px;
    height: 32px;
    min-width: 32px;
    min-height: 32px;
    padding: 0;
    border-left: 1px solid var(--line);
    border-radius: 0;
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: 0.66rem;
  }

  /* Live-mode affordance in the same 32×32 square: a filled dot while connected, a
     muted dot while (re)connecting, a hollow ring once degraded and polling has taken
     over. Shape carries the state as well as color — never color alone. */
  .conn .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
  }

  .conn.is-live {
    color: var(--accent-text);
  }

  .conn.is-degraded {
    color: var(--warn-text);
  }

  .conn.is-degraded .dot {
    background: transparent;
    box-shadow: inset 0 0 0 2px currentColor;
  }

  .refresh {
    position: relative;
    display: grid;
    place-items: center;
    flex: none;
    width: 32px;
    height: 32px;
    min-width: 32px;
    min-height: 32px;
    padding: 0;
    border: 0;
    border-left: 1px solid var(--line);
    border-radius: 0;
    background: transparent;
    color: var(--muted);
    cursor: pointer;
    overflow: hidden;
  }

  .refresh.auto {
    color: var(--accent-text);
  }

  .refresh:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .refresh:focus-visible {
    outline: none;
    box-shadow: inset 0 0 0 2px var(--focus-border);
  }

  .sweep {
    position: absolute;
    inset: -10px;
    background: conic-gradient(from 0deg, transparent 0 290deg, var(--accent-sweep) 360deg);
    animation: sweep-spin 2.6s linear;
    pointer-events: none;
  }

  .glyph {
    position: relative;
    display: grid;
    place-items: center;
  }

  @keyframes sweep-spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
