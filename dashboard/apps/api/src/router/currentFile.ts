import { readdir } from "node:fs/promises";

// Which file is the writer currently appending to? Derived from the writers, not guessed.
//
// ROUTER SINK (`router/patches/0006-request-telemetry-jsonl.patch`). The sink's own
// `TelemetrySink` names files `router-${day}.${index}.jsonl` (patch line 931-933,
// `private fileName(day, index)`), where `day` is the UTC day stamp and `index` is
// `highestIndexForDay(day)` — the MAX NUMERIC index already on disk for that day (patch
// line 943-951), bumped by one whenever the next append would cross `maxBytes` (patch line
// 915-929, `resolveTargetFile`). So the current file is: today's UTC day, highest NUMERIC
// index. Numeric matters — the sink's own `listFiles()` sorts lexicographically (patch line
// 953-963) and uses that order only for retention, so once a day reaches index 10,
// `router-….10.jsonl` sorts BEFORE `router-….2.jsonl` and "lexicographically last" is the
// wrong file. This reader parses the index and compares it as a number.
//
// SIDECAR (`sidecar/codex-sidecar.mjs`) and the SLOTS POLLER
// (`dashboard/scripts/router-slots-poller.mjs`): one file per UTC day, no size rotation —
// `codex-sidecar-YYYY-MM-DD.jsonl` / `slots-YYYY-MM-DD.jsonl`.
//
// Fallback, shared by all three: prefer today's UTC day; if nothing was written today yet
// (an idle router at 00:05 UTC, a poller that is not running), fall back to the newest day
// present rather than reporting `empty`. A stale panel with a visible `sampledAt` is more
// useful than a blank one, and the slots feed's own `stale` flag already says so.

const ROUTER_FILE_PATTERN = /^router-(\d{4}-\d{2}-\d{2})\.(\d+)\.jsonl$/;
const SLOTS_FILE_PATTERN = /^slots-(\d{4}-\d{2}-\d{2})\.jsonl$/;
const SIDECAR_FILE_PATTERN = /^codex-sidecar-(\d{4}-\d{2}-\d{2})\.jsonl$/;

/** `YYYY-MM-DD` in UTC — the same stamp all three writers use. */
export function utcDayStamp(now: Date): string {
  return now.toISOString().slice(0, 10);
}

/** `readdir`, or `null` when the directory is missing/unreadable. Never throws: a
 * configured-but-absent mount is the fleet's stale-mount quirk, and the caller turns it
 * into `{ available: false, reason: "unreadable" }`. */
export async function listDirOrNull(dir: string): Promise<string[] | null> {
  try {
    return await readdir(dir);
  } catch {
    return null;
  }
}

interface Candidate {
  name: string;
  day: string;
  index: number;
}

function pick(candidates: Candidate[], today: string): string | null {
  if (candidates.length === 0) return null;
  const todays = candidates.filter((c) => c.day === today);
  const pool = todays.length > 0 ? todays : candidates;
  let best = pool[0] as Candidate;
  for (const candidate of pool) {
    // ISO day strings compare correctly lexicographically; the index never does.
    if (candidate.day > best.day || (candidate.day === best.day && candidate.index > best.index)) {
      best = candidate;
    }
  }
  return best.name;
}

/** The router sink file the writer is appending to right now (see the header). */
export function selectRouterTelemetryFile(names: string[], now: Date): string | null {
  const candidates: Candidate[] = [];
  for (const name of names) {
    const match = ROUTER_FILE_PATTERN.exec(name);
    if (match) candidates.push({ name, day: match[1] as string, index: Number.parseInt(match[2] as string, 10) });
  }
  return pick(candidates, utcDayStamp(now));
}

/** The slots jsonl the host poller is appending to right now. */
export function selectSlotsFile(names: string[], now: Date): string | null {
  const candidates: Candidate[] = [];
  for (const name of names) {
    const match = SLOTS_FILE_PATTERN.exec(name);
    if (match) candidates.push({ name, day: match[1] as string, index: 0 });
  }
  return pick(candidates, utcDayStamp(now));
}

/** The sidecar jsonl the sidecar is appending to right now. */
export function selectSidecarFile(names: string[], now: Date): string | null {
  const candidates: Candidate[] = [];
  for (const name of names) {
    const match = SIDECAR_FILE_PATTERN.exec(name);
    if (match) candidates.push({ name, day: match[1] as string, index: 0 });
  }
  return pick(candidates, utcDayStamp(now));
}
