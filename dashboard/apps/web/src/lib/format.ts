// Shared display formatters for every view (Runs/Ladders/Long-context/Fit/Serving) --
// pure functions so they're testable without a DOM harness (brief's "component-level tests
// where testable without a DOM harness" pin). Every formatter takes the raw nullable wire
// value and returns the null-safe em-dash rather than each view re-deriving its own guard.

const BYTES_PER_GIB = 1024 ** 3;

export function bytesToGiB(bytes: number | null): number | null {
  return bytes === null ? null : bytes / BYTES_PER_GIB;
}

export function formatGiB(bytes: number | null): string {
  const gib = bytesToGiB(bytes);
  return gib === null ? "—" : `${gib.toFixed(2)} GiB`;
}

/** Signed GiB delta -- Fit view's load/teardown deltas, where the sign itself is
 * information (a negative teardown gap would mean teardown freed MORE than baseline, which
 * is itself worth seeing rather than hiding behind Math.abs). */
export function formatSignedGiB(bytes: number | null): string {
  const gib = bytesToGiB(bytes);
  if (gib === null) return "—";
  const sign = gib > 0 ? "+" : "";
  return `${sign}${gib.toFixed(2)} GiB`;
}

export function formatMs(ms: number | null): string {
  if (ms === null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms.toFixed(0)} ms`;
}

export function formatTokS(tokS: number | null): string {
  return tokS === null ? "—" : `${tokS.toFixed(1)} tok/s`;
}

export function formatPct(rate: number | null): string {
  return rate === null ? "—" : `${(rate * 100).toFixed(1)}%`;
}

export function formatCount(n: number | null): string {
  return n === null ? "—" : String(n);
}

export function formatTimestamp(timestamp: string | null): string {
  if (timestamp === null) return "—";
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? timestamp : date.toLocaleString();
}

/** Short display for a sha256/revision hash -- the first 10 chars plus an ellipsis, never
 * the raw 40/64-char string inline in a table cell. `null` and any string too short to
 * usefully truncate pass through unchanged (a short id truncated to itself plus "..." reads
 * as broken, not compact). */
export function formatShortHash(hash: string | null): string {
  if (hash === null) return "—";
  return hash.length > 12 ? `${hash.slice(0, 10)}…` : hash;
}
