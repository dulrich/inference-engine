//! `ingest/cli.ts parseArgs` cases and `ingest/{run,legacyTiming}.test.ts` over the committed
//! fixture trees.
use super::*;
use crate::record::tests::{fixtures, TempDir};

fn args(a: &[&str]) -> Vec<String> {
    a.iter().map(|s| s.to_string()).collect()
}

#[test]
fn parse_args_cases() {
    let p = |a: &[&str]| parse_args(&args(a), "/default");
    assert_eq!(
        p(&[]),
        Ok(CliOptions {
            dir: "/default".into(),
            rebuild: false
        })
    );
    assert_eq!(
        p(&["--rebuild"]),
        Ok(CliOptions {
            dir: "/default".into(),
            rebuild: true
        })
    );
    assert_eq!(
        p(&["--dir", "/x"]),
        Ok(CliOptions {
            dir: "/x".into(),
            rebuild: false
        })
    );
    assert_eq!(
        p(&["--dir=/y", "--rebuild"]),
        Ok(CliOptions {
            dir: "/y".into(),
            rebuild: true
        })
    );
    assert_eq!(
        p(&["--dir="]),
        Ok(CliOptions {
            dir: String::new(),
            rebuild: false
        })
    );
    // Last --dir wins; a flag-looking value is still the value.
    assert_eq!(p(&["--dir", "/a", "--dir=/b"]).unwrap().dir, "/b");
    assert_eq!(
        p(&["--dir", "--rebuild"]),
        Ok(CliOptions {
            dir: "--rebuild".into(),
            rebuild: false
        })
    );
    assert_eq!(p(&["--dir"]), Err("--dir requires a value".into()));
    assert_eq!(p(&["--bogus"]), Err("unknown argument: --bogus".into()));
    assert_eq!(p(&["--rebuild", "-x"]), Err("unknown argument: -x".into()));
    assert_eq!(
        p(&["--dir", "/a", "extra"]),
        Err("unknown argument: extra".into())
    );
    assert_eq!(p(&["--DIR=/a"]), Err("unknown argument: --DIR=/a".into()));
}

fn temp_db() -> (TempDir, Db) {
    let t = TempDir::new("ingest");
    let db = Db::open(&format!("{}/bench.db", t.0)).unwrap();
    (t, db)
}

/// Content tables minus the AUTOINCREMENT ids and the clock column.
fn content(db: &Db) -> Vec<String> {
    let conn = db.conn();
    let mut out = Vec::new();
    for (table, cols) in [
        ("experiments", "id, description, sweep_server_flags, wilson_summary_json, raw_config_json, raw_prompts_json, schema_origin, model_host_path, battery_json"),
        ("trials", "experiment_id, line_index, type, trial, correct, raw_json"),
        ("metrics", "experiment_id, line_index, metric_type, bytes, raw_json"),
        ("summaries", "experiment_id, n, k, rate, sided, raw_json"),
    ] {
        let mut stmt = conn.prepare(&format!("SELECT {cols} FROM {table}")).unwrap();
        let n = stmt.column_count();
        let mut rows: Vec<String> = stmt
            .query_map([], |r| {
                Ok((0..n)
                    .map(|i| format!("{:?}", r.get::<_, rusqlite::types::Value>(i).unwrap()))
                    .collect::<Vec<_>>()
                    .join("|"))
            })
            .unwrap()
            .map(Result::unwrap)
            .map(|r| format!("{table}:{r}"))
            .collect();
        rows.sort();
        out.extend(rows);
    }
    out
}

#[test]
fn ingests_the_fixture_tree_idempotently() {
    let (_t, db) = temp_db();
    let mut events = Vec::new();
    let s = run_ingest(&db, &fixtures("records"), false, |d, e| {
        events.push((d.to_string(), e.clone()))
    })
    .unwrap();
    assert_eq!(
        s,
        IngestSummary {
            scanned: 3,
            ingested: 2,
            skipped_v1: 1,
            skipped_invalid: 0,
            legacy_timing_ingested: 0,
            rebuilt: false
        }
    );
    assert_eq!(events.iter().filter(|(_, e)| *e == Event::V1).count(), 1);
    assert_eq!(
        db.list_experiment_ids().unwrap(),
        ["fixture-v2-full", "fixture-v2-smoke-cell"]
    );
    let first = content(&db);
    run_ingest(&db, &fixtures("records"), false, |_, _| {}).unwrap();
    assert_eq!(content(&db), first);
    let rebuilt = run_ingest(&db, &fixtures("records"), true, |_, _| {}).unwrap();
    assert!(rebuilt.rebuilt);
    assert_eq!(content(&db), first);
    // Unknown v2 sample types survive into trials.
    let n: i64 = db
        .conn()
        .query_row(
            "SELECT COUNT(*) FROM trials WHERE type = 'future_probe_type'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 1);
}

#[test]
fn legacy_timing_provenance() {
    let (_t, db) = temp_db();
    let s = run_ingest(&db, &fixtures("records-legacy-timing"), false, |_, _| {}).unwrap();
    assert_eq!(
        s,
        IngestSummary {
            scanned: 3,
            ingested: 1,
            skipped_v1: 1,
            skipped_invalid: 0,
            legacy_timing_ingested: 1,
            rebuilt: false
        }
    );
    let origin = |id: &str| -> Option<String> {
        db.conn()
            .query_row(
                "SELECT schema_origin FROM experiments WHERE id = ?",
                [id],
                |r| r.get(0),
            )
            .ok()
    };
    assert_eq!(origin("legacy-timing-full").as_deref(), Some("v1-timing"));
    assert_eq!(origin("v2-control").as_deref(), Some("v2"));
    assert_eq!(origin("legacy-no-timing"), None);
    let trials: i64 = db
        .conn()
        .query_row(
            "SELECT COUNT(*) FROM trials WHERE experiment_id = 'legacy-timing-full'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(trials, 0);
    let first = content(&db);
    run_ingest(&db, &fixtures("records-legacy-timing"), false, |_, _| {}).unwrap();
    assert_eq!(content(&db), first);
}

#[test]
fn two_level_and_errors() {
    let (_t, db) = temp_db();
    let mut invalid = Vec::new();
    let s = run_ingest(&db, &fixtures("records-two-level"), false, |d, e| {
        if let Event::Invalid(r) = e {
            invalid.push((d.to_string(), r.clone()));
        }
    })
    .unwrap();
    assert_eq!(
        s,
        IngestSummary {
            scanned: 5,
            ingested: 0,
            skipped_v1: 3,
            skipped_invalid: 2,
            legacy_timing_ingested: 0,
            rebuilt: false
        }
    );
    assert!(invalid
        .iter()
        .all(|(_, r)| r.starts_with("config.json: ENOENT")));
    // --rebuild clears before an unreadable dir fails the run.
    run_ingest(&db, &fixtures("records"), false, |_, _| {}).unwrap();
    assert!(run_ingest(&db, "/no/such/dir", true, |_, _| {}).is_err());
    assert!(db.list_experiment_ids().unwrap().is_empty());
}
