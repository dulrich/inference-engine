import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { Database } from "bun:sqlite";
import { config } from "../config.ts";
import { applySchema } from "./schema.ts";

// The single owner of the SQLite connection and of schema-application timing (fleet
// convention — model_scoreboard `db/connection.ts`). Nothing else in this codebase
// constructs a `Database`.

export function openDatabase(path: string): Database {
  if (path !== ":memory:" && path !== "") mkdirSync(dirname(path), { recursive: true });
  const db = new Database(path);
  db.exec("PRAGMA foreign_keys = OFF");
  applySchema(db);
  db.exec("PRAGMA foreign_keys = ON");
  return db;
}

/** Process-wide connection, opened once at module load against `config.dbPath`. A `let`
 * with a live ESM binding (not `const`): `useDatabase` rebinds it, and every consumer
 * reads `db` at CALL time inside a function body, so a rebind is picked up by modules
 * imported long before it — the test-isolation seam `db/testDatabase.ts` relies on. */
export let db: Database = openDatabase(config.dbPath);

/** Rebinds the process-wide connection to a fresh database at `path`, closing the
 * previous one, and returns it. Never called in production (`index.ts`/`smoke.ts` open
 * once via `config.dbPath`); `db/testDatabase.ts useTemporaryDatabase()` is the only
 * caller in this codebase's own tests. */
export function useDatabase(path: string): Database {
  const previous = db;
  db = openDatabase(path);
  previous.close();
  return db;
}

export function withTransaction<T>(fn: () => T): T {
  return db.transaction(fn)();
}
