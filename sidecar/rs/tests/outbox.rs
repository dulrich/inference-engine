//! The `serializeRow` byte-equality case of sidecar/test/outbox.test.mjs ("rows are
//! byte-identical to the fleet-tools serializeRow, in a 0600 day-file"). The reference bytes
//! were captured once by running `<code-root>/fleet-tools/scripts/outbox.mjs`
//! `serializeRow({...e, harness: "codex-sidecar"})` under Node on these three events.

use codex_sidecar::outbox::{log_dir_for, serialize_row, OutboxWriter};
use serde_json::{json, Value};
use std::os::unix::fs::PermissionsExt;

const REFERENCE: &str = include_str!("fixtures/serialize-row-reference.jsonl");

fn events() -> Vec<Value> {
    vec![
        json!({"ts": "2026-10-13T01:02:03.456Z", "kind": "turn.completed", "session_id": "s1", "leg": "codex-a",
               "payload": {"stop_reason": "end_turn", "model": "m"}}),
        json!({"ts": "2026-10-13T23:59:59.999Z", "kind": "child.spawned", "session_id": "s2", "leg": "codex-b",
               "agent_id": "0123456789abcdef", "agent_type": "", "payload": {}}),
        json!({"ts": "2026-10-14T00:00:00.000Z", "kind": "usage", "session_id": "s3"}),
    ]
}

#[test]
fn rows_are_byte_identical_to_context_control_serialize_row_in_0600_day_files() {
    let refs: Vec<String> = REFERENCE
        .split_inclusive('\n')
        .map(str::to_string)
        .collect();
    assert_eq!(refs.len(), 3);
    for (e, r) in events().iter().zip(&refs) {
        assert_eq!(&serialize_row(e), r);
    }

    let dir = std::env::temp_dir().join(format!("codex-sidecar-outbox-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let file = dir.join("outbox").join("events.db");
    let file_s = file.to_string_lossy().into_owned();
    let mut w = OutboxWriter::new(&file_s);
    for e in events() {
        assert!(w.record(&e).is_some());
    }
    let log = log_dir_for(&file_s);
    assert_eq!(
        std::fs::metadata(&log).unwrap().permissions().mode() & 0o777,
        0o700
    );
    let day13 = log.join("2026-10-13.jsonl");
    assert_eq!(
        std::fs::metadata(&day13).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        std::fs::read_to_string(&day13).unwrap(),
        format!("{}{}", refs[0], refs[1])
    );
    assert_eq!(
        std::fs::read_to_string(log.join("2026-10-14.jsonl")).unwrap(),
        refs[2]
    );
    assert!(!file.exists(), "the legacy events.db is never created");
    let big = json!({"kind": "turn.completed", "session_id": "s4", "payload": {"big": "x".repeat(70 * 1024)}});
    assert!(w.record(&big).is_none(), "an oversize row is dropped whole");
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn empty_path_writer_is_a_no_op() {
    let mut w = OutboxWriter::new("");
    assert!(w.record(&events()[0]).is_none());
}
