// outbox.mjs — the sidecar's WRITE half of the tracker event seam (plans
// `fleet-tools/plans/tracker-event-seam.md` WP-2, storage replaced by
// `fleet-tools/plans/outbox-append-log.md` WP-2). Rows are appended to JSONL day-files
// shared with the Claude Code hooks; the initiative tracker only ever READS them.
//
// This is a deliberate copy of `fleet-tools/scripts/outbox.mjs` (`serializeRow`,
// `record`, `prune`, `logDirFor`), narrowed to what the sidecar writes. Invariants:
//
//   * **Pinned line format, byte-identical** to the fleet-tools `serializeRow`:
//     `{v, ts, harness, leg, session_id, agent_id, agent_type, kind, payload}` + "\n", absent
//     optional strings `null` — `test/outbox.test.mjs` asserts equality against that file.
//   * **One JSONL day-file per UTC date** of the row's `ts`: `<dirname(path)>/log/<YYYY-MM-DD>.jsonl`.
//     `path` is still `SIDECAR_OUTBOX_PATH` (the legacy `events.db` path); only its directory
//     matters. Dir 0700, files 0600.
//   * **One append write per row**; a row over `LINE_MAX_BYTES` is dropped whole, never split.
//   * **No message content in any row.** Payloads carry ids, model/effort, counts, states
//     and reason words — never a prompt, a completion, or a tool argument.
//   * **Fail-open**: `record` swallows everything. An outbox failure never fails, delays or
//     logs-as-error a request — the same contract as `Logger.write`.
//   * **Import has no side effects**, and an empty `path` opens nothing at all.
//
// Dropped with the SQLite store: the `SCHEMA_SQL` byte-identity, `user_version` stamping,
// `journal_mode=DELETE` and `busy_timeout` — an append-only log has no schema, no journal
// and no lock to wait on (concurrent writers interleave whole lines).

import { appendFileSync, chmodSync, closeSync, mkdirSync, openSync, readdirSync, unlinkSync } from "node:fs";
import { dirname, join } from "node:path";

// The subset of the fleet-tools `EVENT_KINDS` vocabulary this writer emits. A row with
// any other kind is a writer bug, not a row to store.
export const KINDS = Object.freeze(["turn.completed", "usage", "rate_limit", "child.spawned"]);

export const ROW_VERSION = 1;
export const PRUNE_AFTER_MS = 7 * 24 * 60 * 60 * 1000;
export const LINE_MAX_BYTES = 64 * 1024;
// Pruning is opportunistic, not scheduled: roughly one write in fifty pays for it.
const PRUNE_ODDS = 50;
const DAY_FILE_RE = /^(\d{4}-\d{2}-\d{2})\.jsonl$/;
const DAY_MS = 24 * 60 * 60 * 1000;

export function logDirFor(path) {
  return join(dirname(path), "log");
}

function text(value) {
  return typeof value === "string" && value.length > 0 ? value : null;
}

// The pinned line format, trailing newline included (harness is always "codex-sidecar").
export function serializeRow(event = {}) {
  return `${JSON.stringify({
    v: ROW_VERSION,
    ts: text(event.ts) ?? new Date().toISOString(),
    harness: "codex-sidecar",
    leg: text(event.leg),
    session_id: event.session_id,
    agent_id: text(event.agent_id),
    agent_type: text(event.agent_type),
    kind: event.kind,
    payload: event.payload ?? {},
  })}\n`;
}

// Creates the log dir (0700) if needed. Throws on an unwritable path — the caller
// (`createOutboxWriter`) catches and drops the row.
export function openOutbox(path) {
  const dir = logDirFor(path);
  const createdDir = mkdirSync(dir, { recursive: true, mode: 0o700 });
  if (createdDir) {
    // mkdir's mode is masked by umask; restate it. A pre-existing directory is left alone.
    try { chmodSync(dir, 0o700); } catch { /* not ours to tighten */ }
  }
  return dir;
}

function dayOf(name) {
  const match = DAY_FILE_RE.exec(name);
  return match ? Date.parse(`${match[1]}T00:00:00Z`) : NaN;
}

// Unlinks day-files older than retention + 1 day; exported so it can be driven deterministically.
export function prune(logDir, { now = Date.now(), olderThanMs = PRUNE_AFTER_MS } = {}) {
  const cutoff = now - olderThanMs - DAY_MS;
  let removed = 0;
  for (const name of readdirSync(logDir)) {
    if (!(dayOf(name) < cutoff)) continue;
    try { unlinkSync(join(logDir, name)); removed += 1; } catch { /* best-effort */ }
  }
  return removed;
}

// One writer per process. `path` falsy ⇒ every call is a no-op and nothing is ever opened.
// The log dir is created lazily on the first row.
export function createOutboxWriter({ path = "" } = {}) {
  let logDir = null;
  return {
    get path() { return path; },
    record(event = {}) {
      if (!path) return null;
      try {
        const { kind, session_id: sessionId } = event;
        if (!KINDS.includes(kind)) return null;
        if (typeof sessionId !== "string" || sessionId.length === 0) return null;
        const line = serializeRow(event);
        if (Buffer.byteLength(line) > LINE_MAX_BYTES) return null;
        if (!logDir) logDir = openOutbox(path);
        const ms = Date.parse(JSON.parse(line).ts);
        const day = (Number.isFinite(ms) ? new Date(ms) : new Date()).toISOString().slice(0, 10);
        const file = join(logDir, `${day}.jsonl`);
        const fd = openSync(file, "a", 0o600);
        try { appendFileSync(fd, line); } finally { closeSync(fd); }
        if (Math.random() < 1 / PRUNE_ODDS) {
          try { prune(logDir); } catch { /* a failed prune is never a failed write */ }
        }
        return file;
      } catch {
        // Fail-open: an unwritable outbox drops the row, silently.
        return null;
      }
    },
    close() { /* nothing held open between rows */ },
  };
}
