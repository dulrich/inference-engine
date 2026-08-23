// Polling/refresh convention shared across fleet apps. See
// references/polling-refresh-standard.md at the repo root for the full contract.

/** A poll interval in milliseconds. 0 means "manual". */
export type PollOption = number;

/** Convention set (CC-19): Manual / Auto 10s / Auto 30s / Auto 1m. A stored value
 * outside this set (e.g. a legacy 5000) is coerced to the nearest option (ties round
 * up) and re-persisted — see `readStoredIntervalMs`. Only an unparseable/absent stored
 * value falls back to `defaultMs`. */
export const DEFAULT_POLL_OPTIONS: readonly PollOption[] = [0, 10_000, 30_000, 60_000];

export interface CreatePollerOptions {
  /** localStorage key, convention `<app>.<feature>.v1`. */
  storageKey: string;
  /** Called on every tick: interval elapsed, or tab became visible again with polling on. */
  onTick: () => void | Promise<void>;
  /** Overrides DEFAULT_POLL_OPTIONS. The stored/selected value must be one of these. */
  options?: readonly PollOption[];
  /** Used when no valid stored value exists. Defaults to the first option (usually 0/manual). */
  defaultMs?: PollOption;
  /**
   * App-level pause, for when the polled view isn't on screen even though the tab is
   * visible (a non-active route/tab). Distinct from an interval of 0: the user's chosen
   * interval is kept and resumes on unpause. Behaves like tab visibility.
   */
  paused?: boolean;
}

export interface Poller {
  readonly options: readonly PollOption[];
  /** Current validated interval in ms (0 = manual). */
  getIntervalMs(): PollOption;
  /**
   * Validates `ms` against `options` (falls back to the poller's default otherwise),
   * persists it, and restarts the timer. Fires an immediate tick when turning polling on,
   * unless paused.
   */
  setIntervalMs(ms: PollOption): void;
  /**
   * App-level pause. Mirrors tab-visibility semantics: pausing stops the timer, and
   * unpausing fires one immediate tick (the view has been off screen, so its data is
   * stale) and resumes. A no-op when the value hasn't changed, so callers may drive it
   * from a reactive statement that also fires on mount.
   */
  setPaused(paused: boolean): void;
  /** Starts the interval timer if polling is on, not paused, and the tab is visible. */
  start(): void;
  /** Stops the interval timer. Safe to call repeatedly. */
  stop(): void;
  /**
   * Restarts the countdown from a full interval WITHOUT firing a tick. This is the
   * user-click semantics (CC-19): the caller refreshes, then restarts, so the next
   * auto poll lands one full interval after the click.
   */
  restart(): void;
  /**
   * Epoch ms of the next scheduled tick, or undefined when no timer is running
   * (manual, paused, or tab hidden). Drives the countdown segment.
   */
  nextTickAt(): number | undefined;
  /** Stops the timer and removes the visibilitychange listener. Call from onDestroy. */
  destroy(): void;
}

/** Nearest entry in `options` to `value`; ties round up (the larger option wins). */
function nearestSanctioned(value: number, options: readonly PollOption[]): PollOption {
  return options.reduce((best, option) => {
    const diff = Math.abs(value - option);
    const bestDiff = Math.abs(value - best);
    return diff < bestDiff || (diff === bestDiff && option > best) ? option : best;
  });
}

function readStoredIntervalMs(
  storageKey: string,
  options: readonly PollOption[],
  fallback: PollOption,
): PollOption {
  try {
    const raw = localStorage.getItem(storageKey);
    if (raw === null) return fallback;
    const parsed = Number(raw);
    // Unparseable/absent stored value: keep the existing defaultMs fallback.
    if (Number.isNaN(parsed)) return fallback;
    if (options.includes(parsed)) return parsed;
    // Parseable but unsanctioned (e.g. a legacy 5000): coerce to the nearest sanctioned
    // option — ties round up — and persist the coerced value immediately. Never a silent
    // fallback to `defaultMs` for a value the user (or a prior app version) actually set.
    const coerced = nearestSanctioned(parsed, options);
    try {
      localStorage.setItem(storageKey, String(coerced));
    } catch {
      // Best-effort persistence only; the coerced value is still used in-memory.
    }
    return coerced;
  } catch {
    // Ignore invalid/unavailable browser-local storage and use the fallback.
    return fallback;
  }
}

/**
 * Creates a visibility-gated poller: no timer runs while the tab is hidden or the poller
 * is paused, and coming back into view (or unpausing) triggers one immediate refresh and
 * then resumes the interval, if polling is on. A stored interval that parses but isn't one
 * of `options` is coerced to the nearest option and re-persisted (`readStoredIntervalMs`);
 * only an absent or unparseable stored value falls back to `defaultMs`.
 */
export function createPoller(opts: CreatePollerOptions): Poller {
  const options = opts.options ?? DEFAULT_POLL_OPTIONS;
  const defaultMs = opts.defaultMs ?? options[0] ?? 0;

  let intervalMs = readStoredIntervalMs(opts.storageKey, options, defaultMs);
  let paused = opts.paused ?? false;
  let timer: ReturnType<typeof setInterval> | undefined;
  let nextAt: number | undefined;

  function persist() {
    try {
      localStorage.setItem(opts.storageKey, String(intervalMs));
    } catch {
      // Best-effort only; polling still works in-memory without persistence.
    }
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = undefined;
    nextAt = undefined;
  }

  function start() {
    stop();
    if (paused) return;
    if (typeof document !== "undefined" && document.hidden) return;
    if (intervalMs <= 0) return;
    nextAt = Date.now() + intervalMs;
    timer = setInterval(() => {
      nextAt = Date.now() + intervalMs;
      void opts.onTick();
    }, intervalMs);
  }

  function handleVisibilityChange() {
    if (document.hidden) {
      stop();
    } else if (!paused && intervalMs > 0) {
      void opts.onTick();
      start();
    }
  }

  function setIntervalMs(ms: PollOption) {
    intervalMs = options.includes(ms) ? ms : defaultMs;
    persist();
    stop();
    // Changing the interval while paused persists the choice but must not refresh a
    // view that isn't on screen; unpausing is what resumes it.
    if (!paused && intervalMs > 0) {
      void opts.onTick();
      start();
    }
  }

  function setPaused(next: boolean) {
    if (next === paused) return;
    paused = next;
    if (paused) {
      stop();
    } else if (intervalMs > 0) {
      void opts.onTick();
      start();
    }
  }

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", handleVisibilityChange);
  }
  start();

  function destroy() {
    stop();
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    }
  }

  return {
    options,
    getIntervalMs: () => intervalMs,
    setIntervalMs,
    setPaused,
    start,
    stop,
    restart: start,
    nextTickAt: () => nextAt,
    destroy,
  };
}
