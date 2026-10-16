//! `apps/api/src/db/experiments.test.ts`: projections, `percentile`, the served-timing arm pick,
//! the F2 prefill boundary, and modelKey `localeCompare` ordering beyond the fixture keys.
use super::*;
use crate::record::tests::{fixtures, TempDir};
use crate::serving::parse_served_models;
use serde_json::json;

fn temp_db() -> (TempDir, Db) {
    let t = TempDir::new("db");
    let db = Db::open(&format!("{}/bench.db", t.0)).unwrap();
    (t, db)
}

fn ingest(db: &Db, set: &str) {
    crate::ingest::run_ingest(db, &fixtures(set), false, |_, _| {}).unwrap();
}

#[test]
fn percentile_cases() {
    assert_eq!(percentile(&[42.0], 0.5), Some(42.0));
    assert_eq!(percentile(&[42.0], 0.9), Some(42.0));
    assert_eq!(percentile(&[42.0], 0.1), Some(42.0));
    assert_eq!(percentile(&[10.0, 20.0], 0.5), Some(15.0));
    assert_eq!(percentile(&[30.0, 10.0, 20.0], 0.5), Some(20.0));
    let ten: Vec<f64> = (1..=10).map(f64::from).collect();
    assert_eq!(percentile(&ten, 0.5), Some(5.5));
    assert_eq!(percentile(&[10.0, 20.0, 30.0], 0.9), Some(28.0));
    assert_eq!(percentile(&[], 0.5), None);
    assert_eq!(percentile(&[], 0.9), None);
}

#[test]
fn projections_round_trip_through_summary_and_detail() {
    let (_t, db) = temp_db();
    ingest(&db, "records-projections");
    ingest(&db, "records");
    let list = db.list_experiment_summaries().unwrap();
    let battery = list.iter().find(|s| s.id == "fixture-v2-battery").unwrap();
    assert!(battery.sampling_arm.is_some());
    assert!(battery.battery.as_ref().is_some_and(Value::is_array));
    assert!(battery.vram_profile.is_some());
    let legacy = list.iter().find(|s| s.id == "fixture-v2-full").unwrap();
    assert_eq!(
        (
            legacy.sampling_arm.clone(),
            legacy.battery.clone(),
            legacy.vram_profile.clone()
        ),
        (None, None, None)
    );
    assert_eq!(legacy.sweep_cell, None);
    assert!(legacy.has_wilson_summary);
    let detail = db
        .experiment_full_detail("fixture-v2-full")
        .unwrap()
        .unwrap();
    assert_eq!(detail.wilson_summary.as_ref().unwrap()["n"], json!(30));
    assert_eq!(detail.response_caps, None);
    assert_eq!(detail.trials.len() as f64, detail.summary.trial_count);
    assert_eq!(detail.metrics.len() as f64, detail.summary.metric_count);
    let smoke = db
        .experiment_full_detail("fixture-v2-smoke-cell")
        .unwrap()
        .unwrap();
    let cell = smoke.summary.sweep_cell.as_ref().unwrap();
    assert_eq!(cell.cell_id, "fixture-cell-f16-c8192-ctkf16-ctvf16-faon");
    assert!(cell.server_flags.is_object());
    assert!(db.experiment_full_detail("nope").unwrap().is_none());
    assert_eq!(db.experiment_count().unwrap(), 3.0);
}

#[test]
fn response_caps_and_ladder_flags() {
    let (_t, db) = temp_db();
    db.conn()
        .execute_batch(
            r#"INSERT INTO experiments (id, schema_version, hf_repo, sweep_cell_id, sweep_alias, sweep_server_flags, raw_config_json, raw_prompts_json, raw_completions_json, ingested_at)
               VALUES ('a', 2, 'o/r', 'c', NULL, '{"-c":8192,"-ctk":"q8_0","-fa":true,"-ctv":5}', '{"response_caps":{"x":1}}', '[]', '[]', 't'),
                      ('b', 2, 'o/r', NULL, 'al', '', '{"response_caps":[1]}', '[]', '[]', 't');
               INSERT INTO metrics (experiment_id, metric_type, observed_decode_tok_s, first_generation_token_ms, raw_json)
               VALUES ('a', 'timing_round', 10, NULL, '{}'), ('a', 'timing_round', 30, 5, '{}'), ('a', 'vram_loaded', 99, 99, '{}');
               INSERT INTO trials (experiment_id, line_index, type, correct, raw_json)
               VALUES ('a', 0, 'x', 1, '{}'), ('a', 1, 'x', 0, '{}'), ('a', 2, 'x', 1, '{}'), ('a', 3, 'y', NULL, '{}');"#,
        )
        .unwrap();
    let a = db.experiment_full_detail("a").unwrap().unwrap();
    assert_eq!(a.response_caps, Some(json!({"x": 1})));
    assert_eq!(a.summary.sweep_cell.as_ref().unwrap().alias, "");
    assert_eq!(
        db.experiment_full_detail("b")
            .unwrap()
            .unwrap()
            .response_caps,
        None
    );
    let rows = db.list_ladder_rows("o/r").unwrap();
    assert_eq!(rows.len(), 2);
    let r = &rows[0];
    assert_eq!(r.context_length, Some(8192.0));
    assert_eq!(r.kv_cache_type_k.as_deref(), Some("q8_0"));
    assert_eq!(r.kv_cache_type_v, None);
    assert_eq!(r.flash_attention.as_deref(), Some("on"));
    assert_eq!(r.median_decode_tok_s, Some(20.0));
    assert_eq!(r.median_first_generation_token_ms, Some(5.0));
    assert_eq!(r.correctness_rate, Some(2.0 / 3.0));
    assert_eq!(r.trial_count, 4.0);
    assert_eq!(rows[1].context_length, None);
    assert_eq!(rows[1].correctness_rate, None);
    assert!(db.list_ladder_rows("nobody").unwrap().is_empty());
}

const SERVED: &str = r#"
models:
  zeta:
    aliases: [zeta-a]
    cmd: x -m /models/zeta.gguf
  Alpha:
    aliases: [alpha-a]
    cmd: x -m /models/alpha.gguf
  beta_two:
    aliases: [beta-a]
    cmd: x -m /models/beta.gguf
  beta-one:
    aliases: [beta1-a]
    cmd: x -m /models/beta1.gguf
  gamma:
    aliases: [gamma-a]
    cmd: x -m /models/gamma.gguf
"#;

fn exp(db: &Db, id: &str, path: &str, arm: Option<&str>, flags: Option<&str>) {
    db.conn()
        .execute(
            "INSERT INTO experiments (id, schema_version, model_host_path, sampling_arm, sweep_server_flags, raw_config_json, raw_prompts_json, raw_completions_json, ingested_at) VALUES (?, 2, ?, ?, ?, '{}', '[]', '[]', 't')",
            params![id, path, arm, flags],
        )
        .unwrap();
}

fn round(db: &Db, id: &str, decode: Option<f64>, raw: &str) {
    db.conn()
        .execute(
            "INSERT INTO metrics (experiment_id, line_index, metric_type, observed_decode_tok_s, raw_json) VALUES (?, 0, 'timing_round', ?, ?)",
            params![id, decode, raw],
        )
        .unwrap();
}

#[test]
fn serving_timing_arm_pick_prefill_boundary_and_order() {
    let (_t, db) = temp_db();
    let served = parse_served_models(SERVED).unwrap();
    // zeta: production + capability -> production only, never pooled; prefill 100/200/300.
    exp(&db, "z-prod", "/models/zeta.gguf", Some("production"), None);
    exp(&db, "z-cap", "/models/zeta.gguf", Some("capability"), None);
    for (d, p) in [(10.0, 100), (20.0, 200), (30.0, 300)] {
        round(
            &db,
            "z-prod",
            Some(d),
            &format!(r#"{{"server_prompt_eval_tok_s":{p}}}"#),
        );
    }
    round(&db, "z-cap", Some(999.0), "{}");
    // Alpha: capability-only; malformed prefill never admitted, 0 is a sample.
    exp(&db, "a-cap", "/models/alpha.gguf", Some("capability"), None);
    for raw in [
        r#"{"server_prompt_eval_tok_s":"fast"}"#,
        r#"{"server_prompt_eval_tok_s":"123"}"#,
        r#"{"server_prompt_eval_tok_s":true}"#,
        r#"{"server_prompt_eval_tok_s":false}"#,
        r#"{"server_prompt_eval_tok_s":{}}"#,
        r#"{"server_prompt_eval_tok_s":[]}"#,
        r#"{"server_prompt_eval_tok_s":null}"#,
        "{}",
    ] {
        round(&db, "a-cap", Some(40.0), raw);
    }
    round(
        &db,
        "a-cap",
        Some(60.0),
        r#"{"server_prompt_eval_tok_s":0}"#,
    );
    // beta_two: unlabelled only, no prefill at all.
    exp(&db, "b-null", "/models/beta.gguf", None, None);
    round(&db, "b-null", Some(77.0), "{}");
    // beta-one: off-recipe flags only -> no row.
    exp(
        &db,
        "b1-off",
        "/models/beta1.gguf",
        Some("production"),
        Some(r#"{"-ctk":"q4_0","-ctv":"q4_0"}"#),
    );
    round(&db, "b1-off", Some(5.0), "{}");
    // gamma: wrong-typed -ctk -> unresolved -> no row.
    exp(
        &db,
        "g-bad",
        "/models/gamma.gguf",
        Some("production"),
        Some(r#"{"-ctk":7}"#),
    );
    round(&db, "g-bad", Some(5.0), "{}");

    let rows = db.list_serving_timing_rows(&served).unwrap();
    let keys: Vec<&str> = rows.iter().map(|r| r.model_key.as_str()).collect();
    // localeCompare: case-insensitive first; `_` sorts before letters.
    assert_eq!(keys, ["Alpha", "beta_two", "zeta"]);
    let zeta = &rows[2];
    assert_eq!(
        (
            zeta.sampling_arm.as_deref(),
            zeta.sample_count,
            zeta.p50_decode_tok_s,
            zeta.p50_prefill_tok_s,
            zeta.p90_prefill_tok_s
        ),
        (
            Some("production"),
            3.0,
            Some(20.0),
            Some(200.0),
            Some(280.0)
        )
    );
    let alpha = &rows[0];
    assert_eq!(
        (
            alpha.sampling_arm.as_deref(),
            alpha.sample_count,
            alpha.p50_prefill_tok_s,
            alpha.p50_decode_tok_s
        ),
        (Some("capability"), 9.0, Some(0.0), Some(40.0))
    );
    let beta = &rows[1];
    assert_eq!(
        (
            beta.sampling_arm.clone(),
            beta.p50_prefill_tok_s,
            beta.p90_prefill_tok_s,
            beta.p50_decode_tok_s
        ),
        (None, None, None, Some(77.0))
    );
    assert!(db.list_serving_timing_rows(&[]).unwrap().is_empty());
}

#[test]
fn capture_record_reconstructs_samples_in_line_order() {
    let (_t, db) = temp_db();
    ingest(&db, "records");
    let rec = db.capture_record("fixture-v2-full", &[]).unwrap().unwrap();
    assert_eq!(rec.serving, None);
    let lines: Vec<String> = std::fs::read_to_string(format!(
        "{}/fixture-v2-full/samples.jsonl",
        fixtures("records")
    ))
    .unwrap()
    .lines()
    .filter(|l| !l.trim().is_empty())
    .map(|l| crate::jsrt::stringify(&crate::jsrt::json_parse(l).unwrap()))
    .collect();
    let got: Vec<String> = rec.samples.iter().map(crate::jsrt::stringify).collect();
    assert_eq!(got, lines);
    assert!(rec.config.is_object() && rec.prompts.is_array());
    assert!(db.capture_record("nope", &[]).unwrap().is_none());
}
