// Normalized dump of bench.db and the clock normalization of responses (plan WP-1 invariants).
// The DB is read on a host-side copy (db + -wal + -shm, copied out of the bind-mounted /data dir)
// through bun:sqlite in the harness process — never by exec'ing a tool inside the subject (the Rust
// image has neither bun nor sqlite3). Every table sorted by primary key, rows read by column name
// (an ALTER-appended column order is invisible), floats exact.
import { Database } from "bun:sqlite";
import { copyFile, mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { work } from "./lib.ts";

type Row = Record<string, unknown>;
export const CLOCK = "<clock>";
export const ISO_8601 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$/;

/**
 * Process-clock columns, and only those. `experiments.ingested_at` is written as
 * `new Date().toISOString()` by `replaceExperiment` (apps/api/src/db/experiments.ts:223). No other
 * column in apps/api/src/db/schema.ts is written from the clock: trials/metrics/summaries carry
 * record data only (the record's own `timestamp` is data, not process clock).
 */
export const CLOCK_COLUMNS: Record<string, string[]> = { experiments: ["ingested_at"] };

/**
 * Response keys carrying the process clock: `generatedAt` on GET /api/queue
 * (routes/queue.ts `buildQueueListResponse` `new Date().toISOString()`), the capture export
 * (routes/export.ts envelope), router telemetry (router/telemetryFeed.ts:159) and sidecar
 * (router/sidecarFeed.ts:112) and `generated_at` on GET /api/serving/live (serving_live.rs, the
 * reader's clock) — compared by ISO-8601 shape; `ageMs` (no route emits it since the slots feed
 * retired, kept for safety) — compared as a number. `statusGeneratedAt` and every other `*_at`
 * echoed from a fixture file is data and stays exact.
 */
/** The GET /api/serving/live payload (its `generated_at` is the reader clock, not fixture data). */
const isLiveReply = (value: object) => "gpu" in value && "onnx" in value && "recent" in value;

export function normalizeResponse(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalizeResponse);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, v] of Object.entries(value)) {
      if (key === "generatedAt" && typeof v === "string") out[key] = ISO_8601.test(v) ? CLOCK : `not-iso:${v}`;
      else if (key === "generated_at" && typeof v === "string" && isLiveReply(value)) out[key] = ISO_8601.test(v) ? CLOCK : `not-iso:${v}`;
      else if (key === "ageMs" && typeof v === "number") out[key] = CLOCK;
      else out[key] = normalizeResponse(v);
    }
    return out;
  }
  return value;
}

function compareKeys(a: unknown[], b: unknown[]): number {
  for (let i = 0; i < a.length; i += 1) {
    const x = a[i] as number | string;
    const y = b[i] as number | string;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

export interface Dump {
  schema: { tables: Record<string, string[]>; indexes: string[] };
  tables: Record<string, Row[]>;
}

export async function dumpDb(dbFile: string): Promise<Dump> {
  const dir = await mkdtemp(join(work, "dump-"));
  try {
    const copy = join(dir, "bench.db");
    for (const suffix of ["", "-wal", "-shm"]) await copyFile(`${dbFile}${suffix}`, `${copy}${suffix}`).catch(() => undefined);
    const db = new Database(copy);
    try {
      const names = (db.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").all() as { name: string }[]).map((r) => r.name).sort();
      const indexes = (db.query("SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'").all() as { name: string }[]).map((r) => r.name).sort();
      const schema: Dump["schema"] = { tables: {}, indexes };
      const tables: Record<string, Row[]> = {};
      for (const name of names) {
        const info = db.query(`PRAGMA table_info("${name}")`).all() as { name: string; pk: number }[];
        schema.tables[name] = info.map((c) => c.name).sort();
        const pk = info.filter((c) => c.pk > 0).sort((a, b) => a.pk - b.pk).map((c) => c.name);
        const clock = CLOCK_COLUMNS[name] ?? [];
        const rows = db.query(`SELECT * FROM "${name}"`).all() as Row[];
        rows.sort((a, b) => compareKeys(pk.map((k) => a[k]), pk.map((k) => b[k])));
        tables[name] = rows.map((row) => {
          const out: Row = {};
          for (const column of Object.keys(row).sort()) {
            const v = row[column];
            out[column] = clock.includes(column) && v !== null ? (typeof v === "string" && ISO_8601.test(v) ? CLOCK : `not-iso:${String(v)}`) : v;
          }
          return out;
        });
      }
      return { schema, tables };
    } finally {
      db.close();
    }
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}
export const dumpOf = (dataDir: string) => dumpDb(join(dataDir, "bench.db"));

/**
 * Content-only view for idempotency checks: ingest is DELETE-then-INSERT per experiment
 * (ingest/run.ts, db/schema.ts header), so `trials.id`/`metrics.id` (AUTOINCREMENT) advance on
 * every re-run while the content is unchanged. Drops those ids and sorts rows canonically.
 */
export function contentOf(dump: Dump): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const [name, rows] of Object.entries(dump.tables)) {
    out[name] = rows.map((row) => {
      const { id: _id, ...rest } = row;
      return JSON.stringify(name === "trials" || name === "metrics" ? rest : row);
    }).sort();
  }
  return out;
}
