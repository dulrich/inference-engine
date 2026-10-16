//! `queue/eta.test.ts`: the record-timestamp reader, delta retention, median/rounding, basis,
//! and the model-table fallback chain arithmetic.
use super::*;
use crate::record::tests::TempDir;
use serde_json::json;

fn ts(v: &[&str]) -> Vec<String> {
    v.iter().map(|s| s.to_string()).collect()
}

#[test]
fn reads_one_level_of_cell_dirs_skipping_bad_cells() {
    let t = TempDir::new("eta-read");
    t.write(
        "ladder-v1/cell-b/config.json",
        r#"{"timestamp":"2026-08-24T20:10:00.000000+00:00"}"#,
    );
    t.write(
        "ladder-v1/cell-a/config.json",
        r#"{"timestamp":"2026-08-24T20:00:00.000000+00:00"}"#,
    );
    t.write("ladder-v1/cell-bad/config.json", "{not json");
    t.write("ladder-v1/cell-num/config.json", r#"{"timestamp":5}"#);
    t.write("ladder-v1/README.md", "not a cell dir");
    std::fs::create_dir_all(format!("{}/ladder-v1/cell-empty", t.0)).unwrap();
    assert_eq!(
        read_queue_record_timestamps(&t.0, "ladder-v1"),
        ts(&[
            "2026-08-24T20:00:00.000000+00:00",
            "2026-08-24T20:10:00.000000+00:00"
        ])
    );
    assert!(read_queue_record_timestamps(&t.0, "absent").is_empty());
}

#[test]
fn cell_time_stats() {
    let one = compute_cell_time_stats(&ts(&["2026-08-24T20:00:00Z"]));
    assert_eq!((one.median_cell_minutes, one.sample_size), (None, None));
    // Exactly 3600 s is retained; 3601 s is not.
    let s = compute_cell_time_stats(&ts(&["2026-08-24T20:00:00Z", "2026-08-24T21:00:00Z"]));
    assert_eq!(
        (s.median_cell_minutes, s.sample_size),
        (Some(60.0), Some(1.0))
    );
    let s = compute_cell_time_stats(&ts(&["2026-08-24T20:00:00Z", "2026-08-24T21:00:01Z"]));
    assert_eq!(s.median_cell_minutes, None);
    // Even count: mean of the middle pair (5, 10, 15 -> odd 10; 5,15 -> 10).
    let s = compute_cell_time_stats(&ts(&[
        "2026-08-24T20:00:00Z",
        "2026-08-24T20:05:00Z",
        "2026-08-24T20:15:00Z",
        "2026-08-24T20:30:00Z",
    ]));
    assert_eq!(
        (s.median_cell_minutes, s.sample_size),
        (Some(10.0), Some(3.0))
    );
    let s = compute_cell_time_stats(&ts(&[
        "2026-08-24T20:00:00Z",
        "2026-08-24T20:05:00Z",
        "2026-08-24T20:20:00Z",
    ]));
    assert_eq!(s.median_cell_minutes, Some(10.0));
    // Between-window gap dropped; unparseable dropped; unsorted input sorted.
    let s = compute_cell_time_stats(&ts(&[
        "2026-08-23T09:00:00Z",
        "2026-08-23T09:05:00Z",
        "2026-08-24T20:00:00Z",
        "2026-08-24T20:10:00Z",
    ]));
    assert_eq!(
        (s.median_cell_minutes, s.sample_size),
        (Some(7.5), Some(2.0))
    );
    let s = compute_cell_time_stats(&ts(&[
        "not-a-date",
        "2026-08-24T20:00:00Z",
        "2026-08-24T20:05:00Z",
    ]));
    assert_eq!(s.median_cell_minutes, Some(5.0));
    let s = compute_cell_time_stats(&ts(&[
        "2026-08-24T20:10:00Z",
        "2026-08-24T20:00:00Z",
        "2026-08-24T20:05:00Z",
    ]));
    assert_eq!(s.median_cell_minutes, Some(5.0));
}

#[test]
fn basis() {
    assert_eq!(resolve_basis(&[], Some("2026-08-24T20:00:00Z")), None);
    assert_eq!(
        resolve_basis(&ts(&["2026-08-24T20:00:00Z"]), None),
        Some("prior-runs")
    );
    assert_eq!(
        resolve_basis(&ts(&["2026-08-24T20:05:00Z"]), Some("2026-08-24T20:00:00Z")),
        Some("current-run")
    );
    assert_eq!(
        resolve_basis(&ts(&["2026-08-24T20:00:00Z"]), Some("2026-08-24T20:00:00Z")),
        Some("current-run")
    );
    assert_eq!(
        resolve_basis(&ts(&["2026-08-23T20:00:00Z"]), Some("2026-08-24T20:00:00Z")),
        Some("prior-runs")
    );
    assert_eq!(
        resolve_basis(&ts(&["2026-08-24T20:00:00Z"]), Some("not-a-date")),
        Some("prior-runs")
    );
}

#[test]
fn legacy_eta_rounding() {
    let e = compute_queue_eta(
        &ts(&["2026-08-24T20:00:00Z", "2026-08-24T20:10:00Z"]),
        Some(5.0),
        None,
    );
    assert_eq!(
        serde_json::to_value(&e).unwrap(),
        json!({"remainingCells": 5.0, "medianCellMinutes": 10.0, "estimatedMinutesRemaining": 50.0, "cellsPerHour": 6.0, "basis": "prior-runs", "sampleSize": 1.0})
    );
    // 7-minute median: 60/7 = 8.571.. -> 8.6; 3 * 7 = 21.
    let e = compute_queue_eta(
        &ts(&["2026-08-24T20:00:00Z", "2026-08-24T20:07:00Z"]),
        Some(3.0),
        None,
    );
    assert_eq!(
        (e.cells_per_hour, e.estimated_minutes_remaining),
        (Some(8.6), Some(21.0))
    );
    // Zero-length deltas: median 0 -> cellsPerHour null, never Infinity.
    let e = compute_queue_eta(
        &ts(&["2026-08-24T20:00:00.000Z", "2026-08-24T20:00:00.000Z"]),
        Some(2.0),
        None,
    );
    assert_eq!(
        (
            e.median_cell_minutes,
            e.cells_per_hour,
            e.estimated_minutes_remaining
        ),
        (Some(0.0), None, Some(0.0))
    );
    // No state file: remainingCells null -> estimate null.
    let e = compute_queue_eta(
        &ts(&["2026-08-24T20:00:00Z", "2026-08-24T20:10:00Z"]),
        None,
        None,
    );
    assert_eq!(e.estimated_minutes_remaining, None);
    // Rounding is Math.round (halves up): 0.25 * 10 = 2.5 -> 3 -> 0.3.
    assert_eq!(round1(0.25), 0.3);
    assert_eq!(round1(-0.25), -0.2);
}

fn sample(model: &str, context: f64, cost: f64, tokens: Option<f64>) -> CostSample {
    CostSample {
        model: model.into(),
        context,
        cost_seconds: cost,
        avg_prompt_tokens: tokens,
    }
}

fn cell(model: &str, c: f64, state: &str) -> CellDescriptor {
    CellDescriptor {
        model: Some(model.into()),
        c: Some(c),
        state: Some(state.into()),
    }
}

#[test]
fn model_table_fallback_chain() {
    let legacy = compute_queue_eta(
        &ts(&["2026-08-24T20:00:00Z", "2026-08-24T20:10:00Z"]),
        Some(3.0),
        None,
    );
    // No samples or no remaining cells: legacy returned unchanged (no extra keys).
    assert_eq!(
        apply_model_table_eta(legacy.clone(), &[], &[cell("m", 1.0, "pending")]),
        legacy
    );
    assert_eq!(
        apply_model_table_eta(
            legacy.clone(),
            &[sample("m", 1.0, 60.0, None)],
            &[cell("m", 1.0, "completed")]
        ),
        legacy
    );
    let samples = vec![
        sample("m", 8192.0, 600.0, Some(4000.0)),
        sample("m", 8192.0, 1200.0, Some(8000.0)),
        sample("m", 8192.0, 900.0, None),
    ];
    // Tier 1 exact: median(600, 900, 1200) = 900 s = 15 min, 3 samples.
    let e = apply_model_table_eta(legacy.clone(), &samples, &[cell("m", 8192.0, "pending")]);
    assert_eq!(e.basis, Some("model-table"));
    assert_eq!(
        (e.median_cell_minutes, e.estimated_minutes_remaining),
        (Some(15.0), Some(15.0))
    );
    assert_eq!(
        (e.bucket_sample_size, e.scaling_caveat),
        (Some(3.0), Some(false))
    );
    assert_eq!((e.remaining_cells, e.sample_size), (Some(3.0), Some(1.0)));
    // Tier 2 scaled: rates 600/4000 = 0.15, 1200/8000 = 0.15 -> 0.15 * 16384 / 60 = 40.96.
    let e = apply_model_table_eta(legacy.clone(), &samples, &[cell("m", 16384.0, "running")]);
    assert_eq!(
        (e.median_cell_minutes, e.estimated_minutes_remaining),
        (Some(41.0), Some(41.0))
    );
    assert_eq!(
        (e.bucket_sample_size, e.scaling_caveat),
        (Some(2.0), Some(true))
    );
    // Tier 3 legacy (10 min) for an unknown model, mixed with tier 1: median(15, 10) = 12.5.
    let e = apply_model_table_eta(
        legacy.clone(),
        &samples,
        &[
            cell("m", 8192.0, "pending"),
            cell("other", 8192.0, "pending"),
        ],
    );
    assert_eq!(
        (
            e.median_cell_minutes,
            e.estimated_minutes_remaining,
            e.cells_per_hour
        ),
        (Some(12.5), Some(25.0), Some(4.8))
    );
    assert_eq!(e.scaling_caveat, Some(true));
    // Only unknown models: the table resolves nothing -> legacy unchanged.
    assert_eq!(
        apply_model_table_eta(legacy.clone(), &samples, &[cell("other", 1.0, "pending")]),
        legacy
    );
    // Key order on the wire: legacy keys first, the model-table pair appended.
    let e = apply_model_table_eta(legacy, &samples, &[cell("m", 8192.0, "pending")]);
    let keys: Vec<String> = serde_json::to_value(&e)
        .unwrap()
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    assert_eq!(
        keys,
        [
            "remainingCells",
            "medianCellMinutes",
            "estimatedMinutesRemaining",
            "cellsPerHour",
            "basis",
            "sampleSize",
            "bucketSampleSize",
            "scalingCaveat"
        ]
    );
}

#[test]
fn cost_samples_prefer_wall_then_diff_then_probes() {
    let t = TempDir::new("eta-cost");
    let cfg = |extra: Value| {
        let mut v = json!({"sweep_cell": {"model": "m", "server_flags": {"-c": 8192}}});
        v.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        v.to_string()
    };
    t.write("q/a/config.json", &cfg(json!({"cell_wall_seconds": 120.5})));
    t.write("q/b/config.json", &cfg(json!({"cell_started_at": "2026-08-24T20:00:00Z", "cell_ended_at": "2026-08-24T20:02:00Z"})));
    t.write("q/c/config.json", &cfg(json!({})));
    t.write(
        "q/c/samples.jsonl",
        "{\"type\":\"longcontext_probe\",\"wall_seconds\":10,\"prompt_tokens\":100}\nbad\n{\"type\":\"other\",\"wall_seconds\":99}\n {\"type\":\"longcontext_probe\",\"wall_seconds\":5,\"prompt_tokens\":300} \n",
    );
    t.write(
        "q/d/config.json",
        r#"{"sweep_cell":{"server_flags":{"-c":8192}},"cell_wall_seconds":1}"#,
    );
    let s = read_queue_cost_samples(&t.0, "q");
    assert_eq!(
        s,
        vec![
            sample("m", 8192.0, 120.5, None),
            sample("m", 8192.0, 120.0, None),
            sample("m", 8192.0, 15.0, Some(200.0)),
        ]
    );
}
