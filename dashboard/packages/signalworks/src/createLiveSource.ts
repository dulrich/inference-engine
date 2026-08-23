// Live (SSE) refresh transport — the opt-in mode that sits BESIDE the numeric poller
// (CC-19). `createPoller.ts` is untouched by this file: intervals, their storage key and
// `nextTickAt()` stay numeric, and the interval the user picked remains their fallback
// preference while Live is on. See references/polling-refresh-standard.md § Live mode.
//
// Wire contract (named events only — an unnamed `message` is ignored on purpose):
//   event: version   data: <monotonic int | opaque string>   → one refresh per CHANGED value
//   event: heartbeat data: <anything>                        → keepalive, never a refresh
//   retry: <ms>                                              → the server tunes reconnect
//
// Reconnect has exactly one owner: the browser's native EventSource, steered by the
// server's `retry:` field. There is deliberately no backoff loop here — when the source
// stops being OPEN the controller only reports `degraded` so the numeric poller can take
// over, and a lifecycle event (unpause, tab visible, re-enable) is what re-opens it.

/** Minimal `MessageEvent` shape this controller reads — one field, so fakes are trivial. */
export interface LiveMessage {
  readonly data: unknown;
}

/**
 * The slice of the DOM `EventSource` this controller uses. A real `EventSource` satisfies
 * it; WP-3's fake implements these five members and nothing else.
 */
export interface EventSourceLike {
  readonly readyState: number;
  addEventListener(type: string, listener: (event: LiveMessage) => void): void;
  removeEventListener(type: string, listener: (event: LiveMessage) => void): void;
  close(): void;
}

/** Timer/clock seam so the 10s fallback grace is drivable by a fake clock in tests. */
export interface LiveClock {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

/** The `document` members used for visibility gating; injectable for headless tests. */
export interface LiveDocumentLike {
  readonly hidden: boolean;
  addEventListener(type: string, listener: () => void): void;
  removeEventListener(type: string, listener: () => void): void;
}

/** The `localStorage` members used for the enabled flag; injectable for tests. */
export interface LiveStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/**
 * Connection state, and therefore the state of the countdown square's affordance:
 * - `off` — Live is not enabled, or is enabled but suspended (paused / tab hidden).
 * - `connecting` — a source is open-in-progress and still inside the fallback grace.
 * - `live` — the source is OPEN; the numeric poller is suspended.
 * - `degraded` — not OPEN past the grace (or definitively closed); the poller takes over.
 */
export type LiveState = "off" | "connecting" | "live" | "degraded";

export interface CreateLiveSourceOptions {
  /**
   * The PollControl/poller storage key. The enabled flag is persisted beside it under
   * `<storageKey>.live` (`"1"` when on, key removed when off) — the numeric interval
   * under `storageKey` itself is never written by this controller.
   */
  storageKey: string;
  /** SSE endpoint, from the consumer's `liveSource.url`. */
  url: string;
  /** Refresh callback. Fired on `open` and on each CHANGED `version` — never on heartbeats. */
  onTick: () => void | Promise<void>;
  /** Notified on every state transition. Drives PollControl's affordance + poller suspension. */
  onStateChange?: (state: LiveState) => void;
  /** Initial app-level pause (the polled view isn't on screen). Mirrors the poller's `paused`. */
  paused?: boolean;
  /** Grace before a non-OPEN source is called `degraded` and polling resumes. Default 10s. */
  graceMs?: number;
  /** EventSource constructor seam. Defaults to the global `EventSource`. */
  eventSourceFactory?: (url: string) => EventSourceLike;
  /** Clock seam. Defaults to `Date.now` + the global timer functions. */
  clock?: LiveClock;
  /** Visibility seam. Defaults to the global `document` when there is one. */
  doc?: LiveDocumentLike;
  /** Persistence seam. Defaults to the global `localStorage` when there is one. */
  storage?: LiveStorageLike;
}

export interface LiveSource {
  /** The localStorage key holding the enabled flag: `<storageKey>.live`. */
  readonly key: string;
  /** Whether the user has selected Live (persisted; survives reloads). */
  isEnabled(): boolean;
  /** Selects/deselects Live, persists the flag, and opens/closes the source. */
  setEnabled(enabled: boolean): void;
  /** Current connection state. */
  getState(): LiveState;
  /** App-level pause. Closes the source exactly as the poller stops its timer; resume re-opens. */
  setPaused(paused: boolean): void;
  /** Closes the source, clears the grace timer, and drops the visibility listener. */
  destroy(): void;
}

const READY_STATE_OPEN = 1;
const READY_STATE_CLOSED = 2;
/** Fallback grace: the source must be OPEN within this window or polling resumes. */
export const LIVE_FALLBACK_GRACE_MS = 10_000;

const defaultClock: LiveClock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export function createLiveSource(opts: CreateLiveSourceOptions): LiveSource {
  const key = `${opts.storageKey}.live`;
  const graceMs = opts.graceMs ?? LIVE_FALLBACK_GRACE_MS;
  const clock = opts.clock ?? defaultClock;
  const doc = opts.doc ?? (typeof document !== "undefined" ? (document as LiveDocumentLike) : undefined);
  const storage =
    opts.storage ?? (typeof localStorage !== "undefined" ? (localStorage as LiveStorageLike) : undefined);
  const makeSource =
    opts.eventSourceFactory ??
    (typeof EventSource !== "undefined" ? (url: string) => new EventSource(url) as EventSourceLike : undefined);

  let enabled = readEnabled();
  let paused = opts.paused ?? false;
  let state: LiveState = "off";
  let source: EventSourceLike | undefined;
  let graceHandle: unknown;
  /**
   * Last `version` payload seen, as a string. Deliberately NOT reset on reconnect: after a
   * drop the server re-sends the current version, and equality dedupe turns that into a
   * no-op instead of a second refresh on top of the `open` refresh. The very first version
   * of the controller's life is likewise only recorded (`undefined` seed) because the
   * `open` refresh already fetched that state.
   */
  let lastVersion: string | undefined;

  function readEnabled(): boolean {
    try {
      return storage?.getItem(key) === "1";
    } catch {
      // Ignore unavailable/blocked storage; Live simply starts off.
      return false;
    }
  }

  function persistEnabled() {
    try {
      if (enabled) storage?.setItem(key, "1");
      else storage?.removeItem(key);
    } catch {
      // Best-effort persistence only; the in-memory selection still applies.
    }
  }

  function setState(next: LiveState) {
    if (next === state) return;
    state = next;
    opts.onStateChange?.(state);
  }

  function clearGrace() {
    if (graceHandle === undefined) return;
    clock.clearTimeout(graceHandle);
    graceHandle = undefined;
  }

  function armGrace() {
    clearGrace();
    graceHandle = clock.setTimeout(() => {
      graceHandle = undefined;
      // Still not OPEN after the grace: hand the refresh duty back to the numeric poller.
      if (state !== "live") setState("degraded");
    }, graceMs);
  }

  function handleOpen() {
    clearGrace();
    setState("live");
    // One immediate refresh on connect — the view may have gone stale while disconnected.
    // The consumer suspends its poller off this state change.
    void opts.onTick();
  }

  function handleError() {
    if (!source) return;
    if (source.readyState === READY_STATE_OPEN) return; // transient blip on a live stream
    if (source.readyState === READY_STATE_CLOSED) {
      // Native EventSource has given up for good. We do not re-open on a timer (that would
      // be a second reconnect owner); the next lifecycle event re-opens instead.
      closeSource();
      clearGrace();
      setState("degraded");
      return;
    }
    // CONNECTING: native auto-reconnect is in flight. Give it the grace window before the
    // poller takes over, and leave it alone. Arm the grace once per outage — each retry's
    // error event must NOT reset it, or a down server (native retry interval < grace)
    // would re-arm forever, `degraded` would never fire, and the poller would never
    // resume (orchestrator review fix, 2026-08-15).
    if (state !== "degraded") {
      setState("connecting");
      if (graceHandle === undefined) armGrace();
    }
  }

  function handleVersion(event: LiveMessage) {
    const value = typeof event.data === "string" ? event.data : String(event.data);
    if (lastVersion === undefined) {
      lastVersion = value; // seeded by the `open` refresh — not a change
      return;
    }
    if (value === lastVersion) return; // dedupe by equality: one refresh per CHANGED value
    lastVersion = value;
    void opts.onTick();
  }

  function handleHeartbeat() {
    // Keepalive only. Explicitly never a refresh — it exists so idle proxies keep the
    // stream open, and so a silent connection is distinguishable from a dead one.
  }

  function openSource() {
    if (source) return;
    if (!makeSource) {
      // No EventSource in this environment: report degraded so polling covers the surface.
      setState("degraded");
      return;
    }
    const next = makeSource(opts.url);
    source = next;
    next.addEventListener("open", handleOpen);
    next.addEventListener("error", handleError);
    next.addEventListener("version", handleVersion);
    next.addEventListener("heartbeat", handleHeartbeat);
    setState("connecting");
    armGrace();
  }

  function closeSource() {
    const current = source;
    source = undefined;
    if (!current) return;
    current.removeEventListener("open", handleOpen);
    current.removeEventListener("error", handleError);
    current.removeEventListener("version", handleVersion);
    current.removeEventListener("heartbeat", handleHeartbeat);
    current.close();
  }

  function shouldRun(): boolean {
    return enabled && !paused && !(doc?.hidden ?? false);
  }

  function sync() {
    if (shouldRun()) {
      openSource();
      return;
    }
    closeSource();
    clearGrace();
    setState("off");
  }

  function handleVisibilityChange() {
    sync();
  }

  doc?.addEventListener("visibilitychange", handleVisibilityChange);
  sync();

  return {
    key,
    isEnabled: () => enabled,
    setEnabled(next: boolean) {
      if (next === enabled) return;
      enabled = next;
      persistEnabled();
      sync();
    },
    getState: () => state,
    setPaused(next: boolean) {
      if (next === paused) return;
      paused = next;
      sync();
    },
    destroy() {
      closeSource();
      clearGrace();
      doc?.removeEventListener("visibilitychange", handleVisibilityChange);
      state = "off";
    },
  };
}
