import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Database } from "bun:sqlite";

// Test-only support module (fleet convention, model_scoreboard `db/testDatabase.ts`).
// Deliberately NOT named `*.test.ts` — imported BY test files, registers no tests itself,
// never reachable from production code.
//
// Does NOT statically import `./connection.ts`: that module OPENS a database at import
// time (`config.ts` resolves `APP_DB_PATH` at ITS load), so the env var must be written
// before the import happens. The dynamic import below is what makes that ordering
// expressible in one call.

export interface TemporaryDatabase {
  db: Database;
  root: string;
  path: string;
}

/** Gives the CALLING test file its own fresh database and binds the process-wide
 * connection to it, for the duration of that file. Call in the file's module body,
 * before any other import reaching `db/connection.ts`. `bun test` runs one file's body
 * and tests to completion before loading the next, so the binding installed here is the
 * one in force for every test in this file regardless of run order. */
export async function useTemporaryDatabase(label: string): Promise<TemporaryDatabase> {
  const root = mkdtempSync(join(tmpdir(), `bench-dashboard-${label}-`));
  const path = join(root, "app.db");
  process.env.APP_DB_PATH = path;
  const { useDatabase } = await import("./connection.ts");
  return { db: useDatabase(path), root, path };
}
