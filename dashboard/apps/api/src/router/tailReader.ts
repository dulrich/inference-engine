import type { ZodType } from "zod";

// The bounded byte-window tail reader (`plans/router-observability-and-tool-search-conformance.md`
// WP-3). The router sink and the codex sidecar both write append-only jsonl that grows to
// megabytes (the live sink was 4.4 MB on its first day), and BOTH the router container and
// this one are capped at `mem_limit: 256m`. So every read here is
// `Bun.file(path).slice(size - N, size)` for a fixed N — never `.text()` on a whole file.
//
// `apps/api/src/queue/eta.ts:259` is explicitly NOT the precedent: it slurps a whole
// records file via `Bun.file(...).text()`. No streaming seam existed under `apps/api/src`
// before this file; this is it.
//
// Partial-line discipline: a byte window almost always starts mid-line, so the first
// segment is dropped whenever the window was truncated. A TRAILING fragment (writer caught
// mid-append) is deliberately KEPT and left for `parseJsonlLines` to reject as malformed —
// dropping it unconditionally would also drop the last real line of any file that happens
// not to end in a newline.

/** 1 MiB — the telemetry and sidecar window. Measured against the live sink on
 * 2026-09-13: a 1 MiB tail of `router-2026-09-13.0.jsonl` (4.4 MB, progress-heavy)
 * yields 3389 lines = 558 terminal `request` records + 2831 `progress` records, so the
 * window is comfortably above "useful `recent` set" for a full day of traffic. */
export const DEFAULT_TAIL_WINDOW_BYTES = 1024 * 1024;

/** 256 KiB — the slots window. The poller writes one record per tick (5 s default, a few
 * hundred bytes each), and only the NEWEST record is ever used, so a quarter of the
 * telemetry window is generous. */
export const SLOTS_TAIL_WINDOW_BYTES = 256 * 1024;

export interface TailReadResult {
  /** Whole lines, oldest → newest, empty segments removed. */
  lines: string[];
  /** The file's size in bytes at read time. */
  size: number;
  /** The window actually requested. */
  windowBytes: number;
  /** `true` when the file was larger than the window (i.e. a leading partial line was
   * dropped and `lines` is a suffix of the file, not the whole of it). */
  truncated: boolean;
}

/** Reads at most the last `windowBytes` bytes of `path` and returns the whole lines in
 * them. Throws only if the file cannot be opened — callers turn that into a typed
 * `available: false` payload, never a 500. */
export async function readTailLines(
  path: string,
  windowBytes: number = DEFAULT_TAIL_WINDOW_BYTES,
): Promise<TailReadResult> {
  const file = Bun.file(path);
  const size = file.size;
  const start = Math.max(0, size - windowBytes);
  const truncated = start > 0;
  const text = size === 0 ? "" : await file.slice(start, size).text();
  // The window's first line is a fragment of whatever line straddled the offset — drop it.
  // A window that contains no newline at all yields nothing whole, which is honest.
  const body = truncated ? text.slice(text.indexOf("\n") + 1) : text;
  const lines = truncated && !text.includes("\n") ? [] : body.split("\n").filter((line) => line.length > 0);
  return { lines, size, windowBytes, truncated };
}

export interface ParsedJsonl<T> {
  records: T[];
  /** Lines that were not JSON, or were JSON that the schema rejected. Counted rather than
   * thrown: one truncated or drifted line must never cost the whole feed. */
  skipped: number;
}

/** Parses `lines` through `schema`, skipping (and counting) anything malformed. */
export function parseJsonlLines<T>(lines: string[], schema: ZodType<T>): ParsedJsonl<T> {
  const records: T[] = [];
  let skipped = 0;
  for (const line of lines) {
    let raw: unknown;
    try {
      raw = JSON.parse(line);
    } catch {
      skipped += 1;
      continue;
    }
    const parsed = schema.safeParse(raw);
    if (parsed.success) {
      records.push(parsed.data);
    } else {
      skipped += 1;
    }
  }
  return { records, skipped };
}
