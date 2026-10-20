//! `routes/queue.test.ts`: the queue list over the committed fixtures (joined, unjoined,
//! malformed), and the control section incl. the 180 s stale boundary with an injected clock.
use super::*;
use crate::record::tests::{fixtures, TempDir};
use serde_json::json;

fn list() -> Value {
    serde_json::to_value(build_queue_list_response(
        &fixtures("queues"),
        None,
        Value::Null,
    ))
    .unwrap()
}

fn entry<'a>(v: &'a Value, name: &str) -> &'a Value {
    v["queues"]
        .as_array()
        .unwrap()
        .iter()
        .find(|q| q["name"] == name)
        .unwrap()
}

#[test]
fn fixture_queue_list() {
    let v = list();
    let names: Vec<&str> = v["queues"]
        .as_array()
        .unwrap()
        .iter()
        .map(|q| q["name"].as_str().unwrap())
        .collect();
    assert_eq!(
        names,
        [
            "malformed-shape-cells-not-array",
            "malformed-state-pair",
            "malformed-truncated",
            "no-state-queue",
            "synth-joined",
            "synthetic-legacy"
        ]
    );
    assert_eq!(
        entry(&v, "malformed-shape-cells-not-array"),
        &json!({"name": "malformed-shape-cells-not-array", "error": "queue file 'cells' is not an array", "eta": null})
    );
    assert_eq!(
        entry(&v, "malformed-truncated")["error"],
        "JSON Parse error: Unexpected EOF"
    );
    assert_eq!(
        entry(&v, "malformed-state-pair")["error"],
        "JSON Parse error: Expected '}'"
    );
    let none = entry(&v, "no-state-queue");
    assert!(none["cellCounts"]
        .as_object()
        .unwrap()
        .values()
        .all(Value::is_null));
    assert_eq!(none["queueState"], Value::Null);
    let joined = entry(&v, "synth-joined");
    assert_eq!(joined["queueState"], "completed");
    let keys: Vec<&String> = joined["cellCounts"].as_object().unwrap().keys().collect();
    assert_eq!(keys, CELL_STATE_VALUES.to_vec());
    // synthetic-legacy's state file predates top-level aliases: counted, never joined.
    let legacy = entry(&v, "synthetic-legacy");
    assert!(legacy["cells"]
        .as_array()
        .unwrap()
        .iter()
        .all(|c| c["state"].is_null()));
    assert!(legacy["cellCounts"]["completed"].as_f64().unwrap() > 0.0);
}

#[test]
fn missing_queue_dir_is_empty() {
    let r = build_queue_list_response("/nonexistent-bd-queues", None, Value::Null);
    assert!(r.queues.is_empty());
}

fn status(generated_at: &str) -> TempDir {
    let t = TempDir::new("ctl-host");
    let mut v: Value = serde_json::from_str(
        &std::fs::read_to_string(fixtures("control/contract-v1/control/host/status.json")).unwrap(),
    )
    .unwrap();
    v["generated_at"] = json!(generated_at);
    t.write("status.json", &v.to_string());
    t
}

#[test]
fn control_section_stale_boundary_is_exactly_180s() {
    let at = jsrt::date_parse("2026-08-27T12:10:00Z").unwrap();
    let t = status("2026-08-27T12:10:00Z");
    let c = build_control_section(Some(&t.0), at + 180_000.0);
    assert_eq!(c["stale"], false);
    assert_eq!(c["statusGeneratedAt"], "2026-08-27T12:10:00Z");
    assert_eq!(
        build_control_section(Some(&t.0), at + 180_001.0)["stale"],
        true
    );
    let keys: Vec<&String> = c.as_object().unwrap().keys().collect();
    assert_eq!(
        keys,
        [
            "statusGeneratedAt",
            "stale",
            "armed",
            "live",
            "lastRun",
            "anomalies",
            "requests"
        ]
    );
    // Unparseable generated_at: still a section, stale.
    assert_eq!(
        build_control_section(Some(&status("not a date").0), at)["stale"],
        true
    );
}

#[test]
fn control_section_null_cases() {
    assert_eq!(build_control_section(None, 0.0), Value::Null);
    assert_eq!(build_control_section(Some(""), 0.0), Value::Null);
    assert_eq!(
        build_control_section(Some("/nonexistent-bd-host"), 0.0),
        Value::Null
    );
    for dir in ["control/malformed-json", "control/schema-invalid"] {
        assert_eq!(
            build_control_section(Some(&fixtures(dir)), 0.0),
            Value::Null,
            "{dir}"
        );
    }
}

#[test]
fn live_queue_passes_started_at() {
    let control =
        json!({"live": {"queueStem": "synth-joined", "startedAt": "2000-01-01T00:00:00Z"}});
    let r = build_queue_list_response(&fixtures("queues"), Some(&fixtures("records")), control);
    let v = serde_json::to_value(&r).unwrap();
    assert_eq!(v["control"]["live"]["queueStem"], "synth-joined");
}
