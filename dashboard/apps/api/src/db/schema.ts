import type { Database } from "bun:sqlite";

// The database's schema, as ONE baseline — no migration chain (greenfield WP, unlike the
// model_scoreboard exemplar this pattern is borrowed from, which carries retired v1-v9
// history this codebase has none of yet). `applySchema` is `CREATE TABLE IF NOT EXISTS`
// throughout, so it is safe to call on every process boot and every test's temp database.
//
// One exception (WP-A, `plans/bench-dashboard-run-context-surfacing.md`; extended WP-4,
// `plans/bakeoff-timing-records-and-estimates.md`): `CREATE TABLE IF NOT EXISTS` never
// alters an existing table, so columns added to `experiments` after this table's original
// baseline (`sampling_arm`, `battery_json`, `vram_profile`, `schema_origin`) need a GUARDED
// reconciliation step — `PRAGMA table_info` + `ALTER TABLE ... ADD COLUMN` for whichever a
// given database file is still missing (cycle-1 audit B3: `--rebuild` alone only deletes
// rows, it never alters columns, so a pre-existing DB file left untouched by this step
// would 500 on the very first `INSERT` naming a column that doesn't exist yet). Idempotent
// both ways: a brand-new database gets the columns added right after its `CREATE TABLE`
// runs; a pre-existing one gets exactly the columns it's missing and no more on every
// subsequent boot.
//
// Four tables (brief's File scope, apps/api bullet):
//
// - `experiments` — one row per `llama/bakeoff/records/<experiment-id>/config.json`, schema-v2 by
//   default, plus (WP-4) the allowlisted pre-v2 timing-only side door — either way, every
//   row's provenance is explicit in `schema_origin` (`'v2'` default / `'v1-timing'`), never
//   inferred from `schema_version` (which every row here reads `2`, a `replaceExperiment`
//   write-path artifact — see `ingest/record.ts readLegacyTimingRecord`). Every frozen field
//   the config.json carries maps to a named column (lineage's six keys, `sweep_cell`,
//   `wilson_summary`); `raw_config_json` keeps the FULL original object — nothing this
//   schema doesn't name is ever lost. `raw_prompts_json`/`raw_completions_json` hold the
//   parsed `prompts.jsonl`/`completions.jsonl` arrays verbatim (samples.jsonl's own
//   verbatim reconstruction comes from `trials.raw_json`, ordered by `line_index`) — so
//   `GET /api/export/scoreboard-capture`'s raw pass-through is servable from the DB alone,
//   with no live re-read of `llama/bakeoff/records/` at request time. A `v1-timing` row's
//   `raw_prompts_json`/`raw_completions_json` are always `'[]'` — the side door never reads
//   those files, only `samples.jsonl`'s `timing_round` rows.
// - `trials` — one row per `samples.jsonl` line, any `type` (frozen contract: "type is
//   data, not an enum ingest validates against"). `raw_json` is the full original line;
//   the named columns are a best-effort projection so listing/filtering doesn't require
//   parsing JSON in every route.
// - `metrics` — one row per VRAM snapshot (`config.json`'s `vram` object, 0-3 rows) or
//   `timing_round` sample (0-N rows) — the timing/VRAM half of a record, kept separate
//   from `trials`'s correctness/classifier half.
// - `summaries` — at most one row per experiment, populated only when `wilson_summary` is
//   non-null (explicit-null stays absent here too — "no data lives only in the DB" cuts
//   both ways: an explicit null on disk is never turned into a row).
//
// Ingest idempotency (brief: "re-run ingest is always safe … same experiment twice ⇒
// identical content tables") is achieved by DELETE-then-INSERT per experiment inside one
// transaction (`ingest/run.ts`), not by per-row upsert keys — simplest correct answer for
// a derived table whose row set is wholly owned by one experiment_id at a time.

/** Nullable `experiments` columns this WP promotes (`sampling_arm` from `sweep_cell.
 * sampling_arm`, `battery_json` from `sweep_cell.battery` (JSON-stringified), `vram_profile`
 * from top-level `config.vram_profile`) — the guarded `ALTER TABLE` reconciliation list.
 *
 * `schema_origin` (WP-4, `plans/bakeoff-timing-records-and-estimates.md`): provenance for
 * the pre-v2 timing-only ingest side door (`ingest/run.ts`) — `'v2'` (the `DEFAULT`, so
 * every row `replaceExperiment` (`db/experiments.ts`, unowned by this WP) already writes,
 * past and future, reads back `'v2'` without that function ever naming the column) or
 * `'v1-timing'` (set by one explicit follow-up `UPDATE` in `ingest/run.ts`, the sole writer
 * of that value, immediately after the same record's `replaceExperiment` insert). NOT NULL
 * with a DDL default (not merely nullable, unlike its two siblings above) specifically so a
 * pre-existing DB file's reconciled rows read `'v2'`, never `NULL` — "existing rows
 * unaffected (default provenance = v2)" is a value guarantee, not just a schema one.
 *
 * `model_host_path` (WP-1, `plans/served-recipe-p50-prefill-and-decode.md`): the served-
 * recipe resolver's path-join key, promoted from `sweep_cell.model_host_path`
 * (`ingest/record.ts normalizeSweepCell`). Nullable like its `sampling_arm`/`battery_json`/
 * `vram_profile` siblings — a pre-existing row (and any record predating the field, or a
 * v1-timing row, which never carries a path at all) reads back `NULL`, never a guessed
 * value; the closeout's one idempotent ingest replay is what backfills it for every
 * path-bearing v2 record already on disk. */
const RECONCILED_EXPERIMENTS_COLUMNS: readonly { name: string; ddlType: string }[] = [
  { name: "sampling_arm", ddlType: "TEXT" },
  { name: "battery_json", ddlType: "TEXT" },
  { name: "vram_profile", ddlType: "TEXT" },
  { name: "schema_origin", ddlType: "TEXT NOT NULL DEFAULT 'v2'" },
  { name: "model_host_path", ddlType: "TEXT" },
];

/** `PRAGMA table_info(experiments)` + `ALTER TABLE ... ADD COLUMN` for each column in
 * `RECONCILED_EXPERIMENTS_COLUMNS` the table doesn't already have. Safe to call on every
 * boot: a column already present is left untouched, never re-added (that would throw). */
function reconcileExperimentsColumns(db: Database): void {
  const existing = new Set((db.query(`PRAGMA table_info(experiments)`).all() as { name: string }[]).map((c) => c.name));
  for (const column of RECONCILED_EXPERIMENTS_COLUMNS) {
    if (!existing.has(column.name)) {
      db.exec(`ALTER TABLE experiments ADD COLUMN ${column.name} ${column.ddlType}`);
    }
  }
}

export function applySchema(db: Database): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS experiments (
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
  db.exec(`CREATE INDEX IF NOT EXISTS idx_experiments_hf_repo ON experiments(hf_repo)`);
  reconcileExperimentsColumns(db);

  db.exec(`
    CREATE TABLE IF NOT EXISTS trials (
      id                  INTEGER PRIMARY KEY AUTOINCREMENT,
      experiment_id       TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
      line_index          INTEGER NOT NULL,
      type                TEXT NOT NULL,
      trial               INTEGER,
      sampling_arm        TEXT,
      temperature         REAL,
      top_p                REAL,
      top_k                INTEGER,
      sampler_seed        REAL,
      haystack_seed       REAL,
      corpus_manifest_id  TEXT,
      haystack_sha256     TEXT,
      classifier_verdict  TEXT,
      classifier_shape    TEXT,
      prompt_tokens       INTEGER,
      completion_tokens   INTEGER,
      finish_reason       TEXT,
      position_fraction   REAL,
      task                TEXT,
      cell_id             TEXT,
      correct             INTEGER,
      raw_json            TEXT NOT NULL
    )
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_trials_experiment_id ON trials(experiment_id)`);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_trials_type ON trials(type)`);

  db.exec(`
    CREATE TABLE IF NOT EXISTS metrics (
      id                        INTEGER PRIMARY KEY AUTOINCREMENT,
      experiment_id             TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
      line_index                 INTEGER,
      metric_type                TEXT NOT NULL,
      round_type                 TEXT,
      round_index                 INTEGER,
      bytes                      INTEGER,
      first_generation_token_ms  REAL,
      observed_decode_tok_s      REAL,
      prompt_tokens              INTEGER,
      cached_tokens               INTEGER,
      cache_hit_ratio             REAL,
      cache_assertion_passed     INTEGER,
      raw_json                    TEXT NOT NULL
    )
  `);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_metrics_experiment_id ON metrics(experiment_id)`);

  db.exec(`
    CREATE TABLE IF NOT EXISTS summaries (
      experiment_id TEXT PRIMARY KEY REFERENCES experiments(id) ON DELETE CASCADE,
      n             INTEGER,
      k             INTEGER,
      rate          REAL,
      sided         TEXT,
      confidence    REAL,
      lower         REAL,
      upper         REAL,
      raw_json      TEXT NOT NULL
    )
  `);
}
