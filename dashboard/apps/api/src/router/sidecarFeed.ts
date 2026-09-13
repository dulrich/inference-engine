import { join } from "node:path";
import type {
  RouterSidecarError,
  RouterSidecarPersona,
  RouterSidecarRecord,
  RouterSidecarResponse,
} from "@bench-dashboard/shared";
import { RouterSidecarRecordSchema } from "@bench-dashboard/shared";
import { listDirOrNull, selectSidecarFile } from "./currentFile.ts";
import { DEFAULT_TAIL_WINDOW_BYTES, parseJsonlLines, readTailLines } from "./tailReader.ts";

// `GET /api/router/sidecar`'s reader — a bounded tail of the codex sidecar's own jsonl
// (`~/.local/state/codex-sidecar/logs/codex-sidecar-YYYY-MM-DD.jsonl`, bind-mounted
// read-only as `CODEX_SIDECAR_LOG_DIR`), SUMMARISED. The raw lines carry request headers
// and prompt-shaped metadata; none of them is returned on the wire.

/** The kinds that count as an error-ish line. Not an exhaustive kind list — the sidecar's
 * kind set is open and `byKind` counts whatever appears; this is only the error filter. */
export const SIDECAR_ERROR_KINDS: ReadonlySet<string> = new Set([
  "http-error",
  "child-stderr",
  "orphan-tool-result",
]);

/** How many error lines the response carries (newest last). */
export const SIDECAR_MAX_RECENT_ERRORS = 20;

export interface SidecarFeedOptions {
  windowBytes?: number;
  maxRecentErrors?: number;
  /** Injected clock (tests). */
  now?: Date;
}

/** Per-persona latest state. `threadId`/`model` come from the most recent record that
 * carries them (`config-verified` and `http` both do), so a persona whose last line was a
 * bare `count_tokens` still shows the thread it belongs to. */
export function summarisePersonas(records: RouterSidecarRecord[]): RouterSidecarPersona[] {
  const byPersona = new Map<string, RouterSidecarPersona>();
  for (const record of records) {
    const existing = byPersona.get(record.persona);
    const entry: RouterSidecarPersona = existing ?? {
      persona: record.persona,
      threadId: null,
      model: null,
      lastSeenTs: record.ts,
      lastKind: record.kind,
    };
    if (record.threadId !== undefined && record.threadId !== null) entry.threadId = record.threadId;
    if (record.model !== undefined && record.model !== null) entry.model = record.model;
    // Records arrive in file order (oldest first), so the last one wins unconditionally.
    entry.lastSeenTs = record.ts;
    entry.lastKind = record.kind;
    byPersona.set(record.persona, entry);
  }
  return [...byPersona.values()].sort((a, b) => a.persona.localeCompare(b.persona));
}

/** The newest `limit` error-ish lines, oldest first. `message` falls back to `text` —
 * that is where `child-stderr` puts its payload — and is null for `orphan-tool-result`,
 * which carries neither. */
export function collectRecentErrors(records: RouterSidecarRecord[], limit: number): RouterSidecarError[] {
  const errors: RouterSidecarError[] = [];
  for (const record of records) {
    if (!SIDECAR_ERROR_KINDS.has(record.kind)) continue;
    errors.push({
      ts: record.ts,
      persona: record.persona,
      kind: record.kind,
      status: record.status ?? null,
      message: record.message ?? record.text ?? null,
    });
  }
  return errors.slice(Math.max(0, errors.length - limit));
}

/** `GET /api/router/sidecar`'s body. Never throws — same three unavailable reasons as the
 * other two feeds. */
export async function buildRouterSidecarResponse(
  dir: string | undefined,
  options: SidecarFeedOptions = {},
): Promise<RouterSidecarResponse> {
  if (!dir) return { available: false, reason: "disabled" };

  const now = options.now ?? new Date();
  const windowBytes = options.windowBytes ?? DEFAULT_TAIL_WINDOW_BYTES;
  const maxRecentErrors = options.maxRecentErrors ?? SIDECAR_MAX_RECENT_ERRORS;

  const names = await listDirOrNull(dir);
  if (names === null) return { available: false, reason: "unreadable" };

  const file = selectSidecarFile(names, now);
  if (file === null) return { available: false, reason: "empty" };

  let lines: string[];
  try {
    ({ lines } = await readTailLines(join(dir, file), windowBytes));
  } catch {
    return { available: false, reason: "unreadable" };
  }

  const records = parseJsonlLines(lines, RouterSidecarRecordSchema).records;
  if (records.length === 0) return { available: false, reason: "empty" };

  const byKind: Record<string, number> = {};
  for (const record of records) {
    byKind[record.kind] = (byKind[record.kind] ?? 0) + 1;
  }

  return {
    available: true,
    generatedAt: now.toISOString(),
    file,
    byKind,
    personas: summarisePersonas(records),
    recentErrors: collectRecentErrors(records, maxRecentErrors),
  };
}
