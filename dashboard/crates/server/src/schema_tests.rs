//! `apps/api/src/db/schema.test.ts`, plus per-column idempotency on a partially reconciled DB.
use super::*;

const OLD_EXPERIMENTS: &str = "
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
  ";

fn columns(db: &Connection) -> Vec<String> {
    db.prepare("PRAGMA table_info(experiments)")
        .unwrap()
        .query_map([], |r| r.get::<_, String>("name"))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap()
}

fn reconciled(db: &Connection) -> bool {
    let cols = columns(db);
    RECONCILED_EXPERIMENTS_COLUMNS
        .iter()
        .all(|(c, _)| cols.iter().any(|x| x == c))
}

#[test]
fn old_schema_db_gets_every_reconciled_column_with_v2_default() {
    let db = Connection::open_in_memory().unwrap();
    db.execute_batch(OLD_EXPERIMENTS).unwrap();
    db.execute(
        "INSERT INTO experiments (id, schema_version, raw_config_json, raw_prompts_json, raw_completions_json, ingested_at)
         VALUES ('legacy-exp', 2, '{}', '[]', '[]', 'x')",
        [],
    )
    .unwrap();
    apply_schema(&db).unwrap();
    assert!(reconciled(&db));
    let row: (String, Option<String>, Option<String>, Option<String>, String, Option<String>) = db
        .query_row(
            "SELECT id, sampling_arm, battery_json, vram_profile, schema_origin, model_host_path FROM experiments WHERE id = 'legacy-exp'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)),
        )
        .unwrap();
    assert_eq!(
        row,
        ("legacy-exp".into(), None, None, None, "v2".into(), None)
    );
}

#[test]
fn apply_schema_twice_is_idempotent() {
    let db = Connection::open_in_memory().unwrap();
    apply_schema(&db).unwrap();
    apply_schema(&db).unwrap();
    assert!(reconciled(&db));
}

#[test]
fn fresh_db_gets_reconciled_columns_appended_after_the_baseline() {
    let db = Connection::open_in_memory().unwrap();
    apply_schema(&db).unwrap();
    let cols = columns(&db);
    let tail: Vec<&str> = cols[cols.len() - 5..].iter().map(String::as_str).collect();
    assert_eq!(
        tail,
        [
            "sampling_arm",
            "battery_json",
            "vram_profile",
            "schema_origin",
            "model_host_path"
        ]
    );
}

/// A DB where only some reconciled columns exist (an interrupted or older reconciliation): each
/// missing column is added once, the present ones untouched, and a second pass is a no-op.
#[test]
fn partially_reconciled_db_gets_exactly_the_missing_columns() {
    for &(name, ty) in RECONCILED_EXPERIMENTS_COLUMNS {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(OLD_EXPERIMENTS).unwrap();
        db.execute_batch(&format!("ALTER TABLE experiments ADD COLUMN {name} {ty}"))
            .unwrap();
        apply_schema(&db).unwrap();
        let cols = columns(&db);
        for (c, _) in RECONCILED_EXPERIMENTS_COLUMNS {
            assert_eq!(
                cols.iter().filter(|x| x == c).count(),
                1,
                "{c} after {name}"
            );
        }
        assert_eq!(cols.len(), 19 + RECONCILED_EXPERIMENTS_COLUMNS.len());
        apply_schema(&db).unwrap();
        assert_eq!(columns(&db), cols);
    }
}

#[test]
fn open_database_sets_foreign_keys_on_and_creates_the_parent_dir() {
    let root = std::env::temp_dir().join(format!("bd-schema-{}", std::process::id()));
    let path = root.join("nested/bench.db");
    let db = open_database(path.to_str().unwrap()).unwrap();
    let fk: i64 = db
        .query_row("PRAGMA foreign_keys", [], |r| r.get(0))
        .unwrap();
    assert_eq!(fk, 1);
    assert!(reconciled(&db));
    drop(db);
    std::fs::remove_dir_all(&root).unwrap();
}
