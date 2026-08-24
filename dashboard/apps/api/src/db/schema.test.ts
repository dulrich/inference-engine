import { Database } from "bun:sqlite";
import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { applySchema } from "./schema.ts";

// Schema reconciliation (WP-A, cycle-1 audit B3): `applySchema` must upgrade a
// pre-existing DB file created BEFORE `sampling_arm`/`battery_json`/`vram_profile`
// existed -- `CREATE TABLE IF NOT EXISTS` alone never touches an existing table, and
// `--rebuild` (`db/experiments.ts clearAllExperiments`) only deletes rows, it never
// alters columns.

/** The `experiments` table's schema as it existed before this WP -- deliberately NOT
 * derived from `schema.ts` (that would defeat the point of this test: it must exercise
 * the reconciliation path against a table this WP's baseline never created). */
function createOldSchemaExperimentsTable(db: Database): void {
  db.exec(`
    CREATE TABLE experiments (
      id                  TEXT PRIMARY KEY,
      schema_version      INTEGER NOT NULL,
      description         TEXT,
      timestamp           TEXT,
      hf_repo             TEXT,
      hf_revision         TEXT,
      filename            TEXT,
      file_size           INTEGER,
      sha256              TEXT,
      llama_cpp_build     TEXT,
      sweep_cell_id       TEXT,
      sweep_alias         TEXT,
      sweep_server_flags  TEXT,
      sweep_command       TEXT,
      wilson_summary_json TEXT,
      raw_config_json      TEXT NOT NULL,
      raw_prompts_json     TEXT NOT NULL,
      raw_completions_json TEXT NOT NULL,
      ingested_at          TEXT NOT NULL
    )
  `);
}

describe("applySchema: experiments column reconciliation", () => {
  test("adds sampling_arm/battery_json/vram_profile to a pre-existing (old-schema) DB file", () => {
    const root = mkdtempSync(join(tmpdir(), "bench-dashboard-schema-old-"));
    const path = join(root, "old.db");

    const oldDb = new Database(path);
    createOldSchemaExperimentsTable(oldDb);
    oldDb
      .query(
        `INSERT INTO experiments (id, schema_version, raw_config_json, raw_prompts_json, raw_completions_json, ingested_at)
         VALUES (?, 2, '{}', '[]', '[]', ?)`,
      )
      .run("legacy-exp", new Date().toISOString());
    oldDb.close();

    // Re-open the same file (simulating process restart against a pre-existing DB) and
    // run today's `applySchema` against it.
    const reopened = new Database(path);
    expect(() => applySchema(reopened)).not.toThrow();

    const columns = (reopened.query(`PRAGMA table_info(experiments)`).all() as { name: string }[]).map((c) => c.name);
    expect(columns).toContain("sampling_arm");
    expect(columns).toContain("battery_json");
    expect(columns).toContain("vram_profile");

    // Pre-existing row survives reconciliation, new columns read back NULL (never
    // fabricated) for a row ingested before this WP existed.
    const row = reopened
      .query(`SELECT id, sampling_arm, battery_json, vram_profile FROM experiments WHERE id = ?`)
      .get("legacy-exp") as { id: string; sampling_arm: string | null; battery_json: string | null; vram_profile: string | null };
    expect(row.id).toBe("legacy-exp");
    expect(row.sampling_arm).toBeNull();
    expect(row.battery_json).toBeNull();
    expect(row.vram_profile).toBeNull();

    reopened.close();
  });

  test("idempotent: calling applySchema twice on the same (already-reconciled) DB never throws", () => {
    const root = mkdtempSync(join(tmpdir(), "bench-dashboard-schema-idem-"));
    const path = join(root, "app.db");
    const db = new Database(path);
    applySchema(db);
    expect(() => applySchema(db)).not.toThrow();
    const columns = (db.query(`PRAGMA table_info(experiments)`).all() as { name: string }[]).map((c) => c.name);
    expect(columns).toContain("sampling_arm");
    expect(columns).toContain("battery_json");
    expect(columns).toContain("vram_profile");
    db.close();
  });

  test("a brand-new DB gets the reconciled columns too (fresh CREATE TABLE never has them baked in)", () => {
    const root = mkdtempSync(join(tmpdir(), "bench-dashboard-schema-fresh-"));
    const path = join(root, "fresh.db");
    const db = new Database(path);
    applySchema(db);
    const columns = (db.query(`PRAGMA table_info(experiments)`).all() as { name: string }[]).map((c) => c.name);
    expect(columns).toContain("sampling_arm");
    expect(columns).toContain("battery_json");
    expect(columns).toContain("vram_profile");
    db.close();
  });
});
