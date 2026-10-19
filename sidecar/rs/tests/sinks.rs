//! `sinks.mjs` / `http.mjs` pure pieces: the `wireModelId` assertions of `unit.test.mjs`
//! test 5, the message envelope, and Node's `req.headers` shaping.

use codex_sidecar::http::node_headers;
use codex_sidecar::sinks::{message_skeleton, wire_model_id};
use serde_json::{json, Value};

#[test]
fn wire_model_id_restores_the_persona_prefix() {
    let v = |s: &str| Value::from(s);
    assert_eq!(
        wire_model_id("codex-b", Some(&v("gpt-5.6-sol"))),
        "codex-b/gpt-5.6-sol"
    );
    assert_eq!(
        wire_model_id("codex-b", Some(&v("gpt-5.6-sol-high"))),
        "codex-b/gpt-5.6-sol-high"
    );
    assert_eq!(
        wire_model_id("codex-a", Some(&v("codex-a/gpt-5.6-luna"))),
        "codex-a/gpt-5.6-luna"
    );
    // String(requested ?? "")
    assert_eq!(wire_model_id("codex-a", None), "codex-a/");
    assert_eq!(
        wire_model_id("codex-a", Some(&json!(5))),
        "codex-a/5"
    );
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
fn node_headers_join_duplicates_like_node() {
    let mut h = axum::http::HeaderMap::new();
    h.append("accept", "a".parse().unwrap());
    h.append("host", "h1".parse().unwrap());
    h.append("accept", "b".parse().unwrap());
    h.append("host", "h2".parse().unwrap());
    h.append("cookie", "c=1".parse().unwrap());
    h.append("cookie", "d=2".parse().unwrap());
    let m = node_headers(&h);
    assert_eq!(m["accept"], json!("a, b"));
    assert_eq!(m["host"], json!("h1"));
    assert_eq!(m["cookie"], json!("c=1; d=2"));
}
