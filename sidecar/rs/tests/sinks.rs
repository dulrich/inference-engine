//! `sinks` / `http` pure pieces: the wire model id re-prefix, the message envelope, header redaction and the dot-segment 404 rule.

use codex_sidecar::http::has_dot_segment;
use codex_sidecar::logging::redact_headers;
use codex_sidecar::request::Request;
use codex_sidecar::sinks::{message_skeleton, wire_model_id};
use serde_json::{json, Value};

#[test]
fn wire_model_id_restores_the_persona_prefix() {
    assert_eq!(
        wire_model_id("codex-b", "gpt-5.6-sol"),
        "codex-b/gpt-5.6-sol"
    );
    assert_eq!(
        wire_model_id("codex-b", "gpt-5.6-sol-high"),
        "codex-b/gpt-5.6-sol-high"
    );
    assert_eq!(
        wire_model_id("codex-a", "codex-a/gpt-5.6-luna"),
        "codex-a/gpt-5.6-luna"
    );
    // the request's `String(model ?? "")`
    let model = |v: Value| Request::parse(json!({ "model": v })).unwrap().model_text();
    assert_eq!(wire_model_id("codex-a", &model(Value::Null)), "codex-a/");
    assert_eq!(wire_model_id("codex-a", &model(json!(5))), "codex-a/5");
}

#[test]
fn message_skeleton_shape() {
    let m = message_skeleton("codex-a/gpt-5.6-luna");
    let keys: Vec<&String> = m.as_object().unwrap().keys().collect();
    assert_eq!(
        keys,
        [
            "id",
            "type",
            "role",
            "model",
            "content",
            "stop_reason",
            "stop_sequence"
        ]
    );
    let id = m["id"].as_str().unwrap();
    assert!(id.starts_with("msg_") && id.len() == 28, "{id}");
    assert!(id[4..].chars().all(|c| c.is_ascii_hexdigit()));
}

#[test]
fn redacted_headers_come_from_the_header_map() {
    let mut h = axum::http::HeaderMap::new();
    h.append("accept", "a".parse().unwrap());
    h.append("authorization", "Bearer k".parse().unwrap());
    h.append("accept", "b".parse().unwrap());
    h.append("cookie", "c=1".parse().unwrap());
    let m = redact_headers(&h);
    let keys: Vec<&String> = m.keys().collect();
    assert_eq!(keys, ["accept", "authorization", "cookie"]);
    assert_eq!(m["accept"], json!("a, b"));
    assert_eq!(m["authorization"], json!("<redacted>"));
    assert_eq!(m["cookie"], json!("<redacted>"));
}

#[test]
fn dot_segment_paths_are_not_routed() {
    for p in [
        "/codex-a/../healthz",
        "/codex-a/./v1/messages",
        "/%2E%2e/x",
        "/a/.",
    ] {
        assert!(has_dot_segment(p), "{p}");
    }
    for p in ["/codex-a/v1/messages", "/healthz", "/a/..b/.c"] {
        assert!(!has_dot_segment(p), "{p}");
    }
}
