//! `apps/api/src/db/{connection,schema}.ts`: the idempotent baseline (plan Decision 2). The
//! DDL strings are byte-for-byte the Bun API's (SQLite stores them in `sqlite_master`), the
//! guarded `ALTER TABLE experiments ADD COLUMN` reconciliation is the same list in the same
//! order, and the connection runs `foreign_keys` OFF -> schema -> ON. No journal-mode pragma
//! (`connection.ts` sets none).
use rusqlite::Connection;

/// `RECONCILED_EXPERIMENTS_COLUMNS` (`schema.ts`), in order.
pub const RECONCILED_EXPERIMENTS_COLUMNS: &[(&str, &str)] = &[
    ("sampling_arm", "TEXT"),
    ("battery_json", "TEXT"),
    ("vram_profile", "TEXT"),
    ("schema_origin", "TEXT NOT NULL DEFAULT 'v2'"),
    ("model_host_path", "TEXT"),
];

const EXPERIMENTS: &str = "
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
  ";

const TRIALS: &str = "
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
  ";

const METRICS: &str = "
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
  ";

const SUMMARIES: &str = "
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
  ";

/// `reconcileExperimentsColumns`: `PRAGMA table_info` + one `ALTER TABLE ... ADD COLUMN` per
/// missing column; a present column is never re-added.
fn reconcile_experiments_columns(db: &Connection) -> rusqlite::Result<()> {
    let existing: Vec<String> = db
        .prepare("PRAGMA table_info(experiments)")?
        .query_map([], |r| r.get::<_, String>("name"))?
        .collect::<Result<_, _>>()?;
    for (name, ddl_type) in RECONCILED_EXPERIMENTS_COLUMNS {
        if !existing.iter().any(|c| c == name) {
            db.execute_batch(&format!(
                "ALTER TABLE experiments ADD COLUMN {name} {ddl_type}"
            ))?;
        }
    }
    Ok(())
}

/// `applySchema`.
pub fn apply_schema(db: &Connection) -> rusqlite::Result<()> {
    db.execute_batch(EXPERIMENTS)?;
    db.execute_batch("CREATE INDEX IF NOT EXISTS idx_experiments_hf_repo ON experiments(hf_repo)")?;
    reconcile_experiments_columns(db)?;
    db.execute_batch(TRIALS)?;
    db.execute_batch(
        "CREATE INDEX IF NOT EXISTS idx_trials_experiment_id ON trials(experiment_id)",
    )?;
    db.execute_batch("CREATE INDEX IF NOT EXISTS idx_trials_type ON trials(type)")?;
    db.execute_batch(METRICS)?;
    db.execute_batch(
        "CREATE INDEX IF NOT EXISTS idx_metrics_experiment_id ON metrics(experiment_id)",
    )?;
    db.execute_batch(SUMMARIES)?;
    Ok(())
}

/// `openDatabase(path)`: parent dir created (except `:memory:`/`""`), `foreign_keys` OFF ->
/// schema -> ON.
pub fn open_database(path: &str) -> Result<Connection, String> {
    if path != ":memory:" && !path.is_empty() {
        if let Some(parent) = std::path::Path::new(path).parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|e| format!("{path}: {e}"))?;
            }
        }
    }
    let db = Connection::open(path).map_err(|e| format!("{path}: {e}"))?;
    let setup = || -> rusqlite::Result<()> {
        db.execute_batch("PRAGMA foreign_keys = OFF")?;
        apply_schema(&db)?;
        db.execute_batch("PRAGMA foreign_keys = ON")
    };
    setup().map_err(|e| format!("{path}: {e}"))?;
    Ok(db)
}

#[cfg(test)]
#[path = "schema_tests.rs"]
mod tests;
