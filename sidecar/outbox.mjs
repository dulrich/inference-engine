// outbox.mjs — the sidecar's WRITE half of the tracker event seam (plan
// `fleet-tools/plans/tracker-event-seam.md`, WP-2). One SQLite file, shared with the
// Claude Code hooks; the initiative tracker only ever READS it.
//
// This is a deliberate copy of `fleet-tools/scripts/outbox.mjs` (:59-157), narrowed to
// what the sidecar writes. Invariants it must keep:
//
//   * **`SCHEMA_SQL` is byte-identical** to the fleet-tools export — `test/outbox.test.mjs`
//     asserts it against that file. Never reformat it.
//   * **Writer-owned, idempotent schema**: `CREATE TABLE IF NOT EXISTS` on every open, no
//     migration framework; `PRAGMA user_version` stamped ONLY when it reads 0, so a writer
//     that has already moved the file to v2 is never demoted.
//   * **`journal_mode=DELETE`, never WAL** — the tracker sees the repo through a read-only
//     bind mount, where a WAL database is unreadable once the writer has closed.
//   * **No message content in any row.** Payloads carry ids, model/effort, counts, states
//     and reason words — never a prompt, a completion, or a tool argument.
//   * **Fail-open**: `record` swallows everything. An outbox failure never fails, delays or
//     logs-as-error a request — the same contract as `Logger.write`.
//   * **Import has no side effects**, and an empty `path` opens nothing at all.

import { DatabaseSync } from "node:sqlite";
import { chmodSync, existsSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

export const SCHEMA_VERSION = 1;

export const SCHEMA_SQL = `CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  harness TEXT NOT NULL CHECK(harness IN ('claude','codex-sidecar')),
  leg TEXT,
  session_id TEXT NOT NULL,
  agent_id TEXT,
  agent_type TEXT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session_seq ON events (session_id, seq);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
`;

const INSERT_SQL =
  "INSERT INTO events (ts, harness, leg, session_id, agent_id, agent_type, kind, payload)" +
  " VALUES (?, ?, ?, ?, ?, ?, ?, ?)";

// The subset of the fleet-tools `EVENT_KINDS` vocabulary this writer emits. A row with
// any other kind is a writer bug, not a row to store.
export const KINDS = Object.freeze(["turn.completed", "usage", "rate_limit", "child.spawned"]);

export const PRUNE_AFTER_MS = 7 * 24 * 60 * 60 * 1000;
// Pruning is opportunistic, not scheduled: roughly one write in fifty pays for it.
const PRUNE_ODDS = 50;

// Opens (creating if needed) the outbox for writing. Throws on an unwritable path — the
// caller (`createOutboxWriter`) catches and drops the row.
export function openOutbox(path, { busyTimeoutMs = 250 } = {}) {
  const createdDir = mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  if (createdDir) {
    // mkdir's mode is masked by umask; restate it. A pre-existing directory is left alone.
    try { chmodSync(createdDir, 0o700); } catch { /* not ours to tighten */ }
  }
  const fresh = !existsSync(path);
  const db = new DatabaseSync(path);
  if (fresh) {
    try { chmodSync(path, 0o600); } catch { /* still works for the reader */ }
  }
  db.exec("PRAGMA journal_mode = DELETE");
  db.exec(`PRAGMA busy_timeout = ${Math.max(0, Math.trunc(Number(busyTimeoutMs) || 0))}`);
  db.exec(SCHEMA_SQL);
  const row = db.prepare("PRAGMA user_version").get();
  if (Number(row?.user_version ?? 0) === 0) db.exec(`PRAGMA user_version = ${SCHEMA_VERSION}`);
  return db;
}

function text(value) {
  return typeof value === "string" && value.length > 0 ? value : null;
}

// Drops rows older than the retention window; exported so it can be driven deterministically.
export function prune(db, { now = Date.now(), olderThanMs = PRUNE_AFTER_MS } = {}) {
  const cutoff = new Date(now - olderThanMs).toISOString();
  return db.prepare("DELETE FROM events WHERE ts < ?").run(cutoff).changes;
}

// One writer per process. `path` falsy ⇒ every call is a no-op and nothing is ever opened.
// The database is opened lazily on the first row and kept for the process lifetime.
export function createOutboxWriter({ path = "", busyTimeoutMs = 250 } = {}) {
  let db = null;
  return {
    get path() { return path; },
    record(event = {}) {
      if (!path) return null;
      try {
        const { kind, session_id: sessionId } = event;
        if (!KINDS.includes(kind)) return null;
        if (typeof sessionId !== "string" || sessionId.length === 0) return null;
        if (!db) db = openOutbox(path, { busyTimeoutMs });
        const info = db.prepare(INSERT_SQL).run(
          text(event.ts) ?? new Date().toISOString(),
          "codex-sidecar",
          text(event.leg),
          sessionId,
          text(event.agent_id),
          text(event.agent_type),
          kind,
          JSON.stringify(event.payload ?? {}),
        );
        if (Math.random() < 1 / PRUNE_ODDS) {
          try { prune(db); } catch { /* a failed prune is never a failed write */ }
        }
        return Number(info.lastInsertRowid);
      } catch {
        // Fail-open: an unwritable, locked or corrupt outbox drops the row, silently.
        return null;
      }
    },
    close() {
      if (!db) return;
      try { db.close(); } catch { /* ignore */ }
      db = null;
    },
  };
}
