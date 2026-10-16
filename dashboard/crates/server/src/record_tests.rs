//! `apps/api/src/ingest/{record,legacyTiming}.test.ts` cases, over the committed
//! `fixtures/` tree, plus per-field projection and invalid-reason cases the black-box
//! fixtures do not reach.
use super::*;
use serde_json::json;

pub(crate) fn fixtures(set: &str) -> String {
    jsrt::path_join(env!("CARGO_MANIFEST_DIR"), &format!("../../fixtures/{set}"))
}

fn v2(dir: &str) -> NormalizedRecord {
    match read_record_dir(dir) {
        ReadResult::V2(r) => *r,
        other => panic!("{dir}: {other:?}"),
    }
}

pub(crate) struct TempDir(pub String);
impl TempDir {
    pub(crate) fn new(tag: &str) -> TempDir {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static N: AtomicUsize = AtomicUsize::new(0);
        let p = std::env::temp_dir().join(format!(
            "bd-{tag}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&p).unwrap();
        TempDir(p.to_string_lossy().into_owned())
    }
    pub(crate) fn write(&self, rel: &str, text: &str) {
        let p = Path::new(&self.0).join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, text).unwrap();
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn record_dir(config: Value, samples: &str) -> TempDir {
    let t = TempDir::new("rec");
    t.write("r/config.json", &config.to_string());
    t.write("r/prompts.jsonl", "");
    t.write("r/completions.jsonl", "");
    t.write("r/samples.jsonl", samples);
    t
}

#[test]
fn classifies_v1_and_the_smoke_cell_v2() {
    assert_eq!(
        read_record_dir(&fixtures("records/fixture-v1-legacy")),
        ReadResult::V1
    );
    let r = v2(&fixtures("records/fixture-v2-smoke-cell"));
    assert_eq!(r.id, "fixture-v2-smoke-cell");
    assert_eq!(r.hf_repo.as_deref(), Some("example-org/Example-Tiny-GGUF"));
    assert_eq!(
        r.sweep_cell_id.as_deref(),
        Some("fixture-cell-f16-c8192-ctkf16-ctvf16-faon")
    );
    assert_eq!(r.sweep_alias.as_deref(), Some("fixture-alias-f16-c8192"));
    assert_eq!(r.wilson_summary, None);
    assert_eq!(r.trials.len(), 3);
    assert!(r.trials.iter().all(|t| t.kind == "sampling_echo_check"));
    assert_eq!(r.metrics.len(), 3);
    assert!(r.metrics.iter().all(|m| m.metric_type.starts_with("vram_")));
    assert_eq!(r.model_host_path, None);
    assert_eq!(r.sampling_arm, None);
    assert_eq!(r.battery, None);
}

#[test]
fn full_record_projection() {
    let r = v2(&fixtures("records/fixture-v2-full"));
    assert_eq!(r.sweep_cell_id, None);
    assert_eq!(r.sweep_alias, None);
    assert_eq!(
        r.wilson_summary,
        Some(
            json!({"n": 30, "k": 0, "rate": 0, "sided": "one", "confidence": 0.95,
            "lower": null, "upper": 0.08266815016452403})
        )
    );
    let unknown = r
        .trials
        .iter()
        .find(|t| t.kind == "future_probe_type")
        .unwrap();
    assert_eq!(unknown.raw["some_future_field"], json!(42));
    assert!(!r.trials.iter().any(|t| t.kind == "timing_round"));
    assert_eq!(
        r.metrics
            .iter()
            .filter(|m| m.metric_type == "timing_round")
            .count(),
        2
    );
    let lc: Vec<_> = r
        .trials
        .iter()
        .filter(|t| t.kind == "longcontext_probe")
        .collect();
    assert_eq!(lc.len(), 8);
    assert!(lc
        .iter()
        .all(|t| t.task.as_deref() == Some("single_needle")));
    let mut pos: Vec<f64> = lc.iter().filter_map(|t| t.position_fraction).collect();
    pos.dedup();
    pos.sort_by(f64::total_cmp);
    pos.dedup();
    assert_eq!(pos, vec![0.1, 0.45]);
}

#[test]
fn battery_projection_fields() {
    let r = v2(&fixtures("records-projections/fixture-v2-battery"));
    assert!(r.sampling_arm.is_some());
    assert!(r.battery.is_some());
    assert!(r.vram_profile.is_some());
}

#[test]
fn missing_config_is_invalid_with_the_bun_enoent_reason() {
    let dir = fixtures("records/does-not-exist");
    assert_eq!(
        read_record_dir(&dir),
        ReadResult::Invalid(format!(
            "config.json: ENOENT: no such file or directory, open '{dir}/config.json'"
        ))
    );
}

#[test]
fn invalid_reasons() {
    let t = TempDir::new("inv");
    t.write("arr/config.json", "[1,2]");
    t.write("bad/config.json", "{");
    let d = |n: &str| format!("{}/{n}", t.0);
    assert_eq!(
        read_record_dir(&d("arr")),
        ReadResult::Invalid("config.json is not a JSON object".into())
    );
    match read_record_dir(&d("bad")) {
        ReadResult::Invalid(r) => assert!(r.starts_with("config.json: "), "{r}"),
        other => panic!("{other:?}"),
    }
    // A v2 config whose prompts.jsonl is absent: Bun's ENOENT text for the jsonl path.
    t.write("noprompts/config.json", r#"{"schema_version":2}"#);
    assert_eq!(
        read_record_dir(&d("noprompts")),
        ReadResult::Invalid(format!(
            "ENOENT: no such file or directory, open '{}/prompts.jsonl'",
            d("noprompts")
        ))
    );
    // A malformed jsonl line names the path and the 1-based non-blank line number.
    let r = record_dir(json!({"schema_version": 2}), "{\"a\":1}\n\n   \n{oops\n");
    match read_record_dir(&format!("{}/r", r.0)) {
        ReadResult::Invalid(reason) => {
            assert!(
                reason.starts_with(&format!("{}/r/samples.jsonl:2: ", r.0)),
                "{reason}"
            )
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn wilson_summary_validation() {
    let bad = |w: Value| {
        let t = record_dir(
            json!({"schema_version": 2, "lineage": {}, "sweep_cell": null, "wilson_summary": w}),
            "",
        );
        match read_record_dir(&format!("{}/r", t.0)) {
            ReadResult::Invalid(reason) => {
                assert!(
                    reason.starts_with("config.json: wilson_summary: "),
                    "{reason}"
                )
            }
            other => panic!("{other:?}"),
        }
    };
    bad(
        json!({"n": "thirty", "k": 0, "rate": 0, "sided": "one", "confidence": 0.95, "lower": null, "upper": null}),
    );
    bad(
        json!({"n": 30, "rate": 0, "sided": "one", "confidence": 0.95, "lower": null, "upper": null}),
    );
    bad(
        json!({"n": 30, "k": 0, "rate": 0, "sided": "three", "confidence": 0.95, "lower": null, "upper": null}),
    );
    let t = record_dir(json!({"schema_version": 2, "wilson_summary": null}), "");
    assert_eq!(v2(&format!("{}/r", t.0)).wilson_summary, None);
    // Valid non-null: the parse output, unknown keys stripped, declared key order.
    let t = record_dir(
        json!({"schema_version": 2, "wilson_summary": {"upper": 0.27, "extra": 1, "n": 30, "k": 3,
            "rate": 0.1, "sided": "two", "confidence": 0.95, "lower": 0.02}}),
        "",
    );
    let w = v2(&format!("{}/r", t.0)).wilson_summary.unwrap();
    assert_eq!(
        jsrt::stringify(&w),
        r#"{"n":30,"k":3,"rate":0.1,"sided":"two","confidence":0.95,"lower":0.02,"upper":0.27}"#
    );
}

/// Per-field projection: type-checked promotion, never coercion; `trial_index` fallback;
/// unknown `type` and non-object rows become `"unknown"` trials; `schema_version: 2.0` is v2.
#[test]
fn per_field_projection() {
    let samples = [
        json!({"type": "needle", "trial": "1", "trial_index": 4, "temperature": 0.7, "top_k": 40,
            "correct": true, "classifier_verdict": 3, "position_fraction": 0.25, "task": "t"}),
        json!([1, 2]),
        json!({"type": "timing_round", "run_state": "warm", "round_index": 2,
            "first_generation_token_ms": 12.5, "cache_assertion_passed": false, "prompt_tokens": "9"}),
        json!({"correct": 1}),
    ]
    .iter()
    .map(Value::to_string)
    .collect::<Vec<_>>()
    .join("\n");
    let t = record_dir(
        json!({"schema_version": 2.0, "description": 5, "lineage": {"hf_repo": "o/r", "file_size": 1.5e9},
            "sweep_cell": {"cell_id": "c", "alias": 7, "server_flags": {"-c": 8192}, "battery": ["a", 1],
                "model_host_path": "/m/x.gguf", "sampling_arm": "production"},
            "vram": {"baseline_bytes": 10, "loaded_bytes": "x", "after_teardown_bytes": 3}}),
        &samples,
    );
    let r = v2(&format!("{}/r", t.0));
    assert_eq!(r.description, None);
    assert_eq!(r.hf_repo.as_deref(), Some("o/r"));
    assert_eq!(r.file_size, Some(1.5e9));
    assert_eq!(r.sweep_alias, None);
    assert_eq!(r.sweep_server_flags, Some(json!({"-c": 8192})));
    assert_eq!(r.battery, None);
    assert_eq!(r.model_host_path.as_deref(), Some("/m/x.gguf"));
    assert_eq!(r.sampling_arm.as_deref(), Some("production"));
    let t0 = &r.trials[0];
    assert_eq!(
        (t0.line_index, t0.trial, t0.top_k),
        (0, Some(4.0), Some(40.0))
    );
    assert_eq!(
        (t0.correct, t0.classifier_verdict.clone()),
        (Some(true), None)
    );
    assert_eq!(r.trials[1].kind, "unknown");
    assert_eq!(r.trials[1].line_index, 1);
    assert_eq!(r.trials[2].kind, "unknown");
    assert_eq!(r.trials[2].correct, None);
    let vram: Vec<_> = r
        .metrics
        .iter()
        .filter(|m| m.line_index.is_none())
        .collect();
    assert_eq!(
        vram.iter()
            .map(|m| (m.metric_type.as_str(), m.bytes))
            .collect::<Vec<_>>(),
        vec![
            ("vram_baseline", Some(10.0)),
            ("vram_after_teardown", Some(3.0))
        ]
    );
    let timing = r.metrics.iter().find(|m| m.line_index == Some(2)).unwrap();
    assert_eq!(timing.round_type.as_deref(), Some("warm"));
    assert_eq!(timing.round_index, Some(2.0));
    assert_eq!(timing.prompt_tokens, None);
    assert_eq!(timing.cache_assertion_passed, Some(false));
}

#[test]
fn legacy_timing_side_door() {
    let lt = |n: &str| read_legacy_timing_record(&fixtures(&format!("records-legacy-timing/{n}")));
    let LegacyResult::V1Timing(r) = lt("legacy-timing-full") else {
        panic!("legacy-timing-full")
    };
    assert!(r.trials.is_empty());
    assert!(!r.metrics.is_empty());
    assert!(r.metrics.iter().all(|m| m.metric_type == "timing_round"));
    assert!(r.raw_prompts.is_empty() && r.raw_completions.is_empty());
    assert_eq!(r.wilson_summary, None);
    assert!(r.sweep_alias.is_some());
    assert_eq!(r.model_host_path, None);
    assert_eq!(lt("legacy-no-timing"), LegacyResult::V1);
    assert_eq!(lt("v2-control"), LegacyResult::V1);
    assert_eq!(
        read_legacy_timing_record(&fixtures("records/fixture-v1-legacy")),
        LegacyResult::V1
    );
    // Alias falls back to sweep_cell.alias; top-level wins.
    let t = TempDir::new("lt");
    t.write("a/config.json", r#"{"sweep_cell":{"alias":"cell"}}"#);
    t.write("a/samples.jsonl", "{\"type\":\"timing_round\"}\n");
    t.write(
        "b/config.json",
        r#"{"alias":"top","sweep_cell":{"alias":"cell"}}"#,
    );
    t.write("b/samples.jsonl", "{\"type\":\"timing_round\"}\n{bad\n");
    match read_legacy_timing_record(&format!("{}/a", t.0)) {
        LegacyResult::V1Timing(r) => assert_eq!(r.sweep_alias.as_deref(), Some("cell")),
        other => panic!("{other:?}"),
    }
    assert!(matches!(
        read_legacy_timing_record(&format!("{}/b", t.0)),
        LegacyResult::Invalid(_)
    ));
}

#[test]
fn two_level_scan() {
    let root = fixtures("records-two-level");
    let names: Vec<String> = list_record_dirs(&root)
        .unwrap()
        .iter()
        .map(|d| d[root.len() + 1..].to_string())
        .collect();
    assert_eq!(
        names,
        vec![
            "batch-run/cell-a",
            "batch-run/cell-b",
            "flat-record",
            "neither-dir",
            "nested-too-deep"
        ]
    );
    let flat: Vec<String> = list_record_dirs(&fixtures("records"))
        .unwrap()
        .iter()
        .map(|d| jsrt::basename(d).to_string())
        .collect();
    assert_eq!(
        flat,
        vec![
            "fixture-v1-legacy",
            "fixture-v2-full",
            "fixture-v2-smoke-cell"
        ]
    );
    assert_eq!(
        list_record_dirs("/no/such/records"),
        Err("ENOENT: no such file or directory, scandir '/no/such/records'".into())
    );
}
