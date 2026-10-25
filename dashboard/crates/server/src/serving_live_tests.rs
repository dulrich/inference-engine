//! `/api/serving/live` over the committed `fixtures/serving-feed/` feed and temp feeds.
use super::*;
use crate::test_support::{fixtures, TempDir};
use bd_domain::contract::SERVING_LIVE_RESPONSE;
use bd_domain::validate;

fn ms(iso: &str) -> f64 {
    jsrt::date_parse(iso).unwrap()
}

fn feed() -> String {
    fixtures("serving-feed")
}

const NOW: &str = "2026-10-19T12:00:40.000Z";

#[test]
fn live_over_fixture_feed() {
    let r = build_serving_live_response(Some(&feed()), ms(NOW));
    assert_eq!(validate::parse(&SERVING_LIVE_RESPONSE.node, &r).unwrap(), r);
    assert_eq!(r["available"], true);
    assert_eq!(r["file"], "serving-feed-2026-10-19.jsonl");
    assert_eq!(r["generated_at"], NOW);
    assert_eq!(
        r["gpu"],
        json!({"available": true, "sampled_at": "2026-10-19T12:00:30.000Z", "models": [
            {"model": "qwen3.6-35b-a3b", "state": "ready", "ttl": 1800, "preload": null,
             "idle_unload_seconds": null}]})
    );
    // The latest onnx snapshot is the unreachable one: typed per-tier, not a blank.
    assert_eq!(r["onnx"]["available"], false);
    assert_eq!(r["onnx"]["sampled_at"], "2026-10-19T12:00:30.000Z");
    assert!(r["onnx"]["reason"]
        .as_str()
        .unwrap()
        .starts_with("unreachable"));
    // Newest first; the foreign-schema and non-JSON lines are skipped.
    assert_eq!(
        r["recent"],
        json!([
            {"ts": "2026-10-19T12:00:30.000Z", "tier": "gpu", "kind": "unload", "model": "bge-m3-f16"},
            {"ts": "2026-10-19T12:00:15.000Z", "tier": "onnx", "kind": "load", "model": "gliner2-base"},
            {"ts": "2026-10-19T12:00:15.000Z", "tier": "gpu", "kind": "load", "model": "qwen3.6-35b-a3b"},
        ])
    );
}

#[test]
fn typed_unavailable_states() {
    let off = json!({"available": false, "reason": "disabled"});
    assert_eq!(build_serving_live_response(None, ms(NOW)), off);
    assert_eq!(build_serving_live_response(Some(""), ms(NOW)), off);
    assert_eq!(
        build_serving_live_response(Some("/nonexistent/serving-feed"), ms(NOW))["reason"],
        "unreadable"
    );
    let t = TempDir::new("serving-live-empty");
    assert_eq!(
        build_serving_live_response(Some(&t.0), ms(NOW))["reason"],
        "empty"
    );
    t.write("serving-feed-2026-10-19.jsonl", "garbage\n");
    assert_eq!(
        build_serving_live_response(Some(&t.0), ms(NOW))["reason"],
        "empty"
    );
    // 60 s after the newest snapshot (four 15 s polls) the feed is stale.
    let at = |iso: &str| build_serving_live_response(Some(&feed()), ms(iso));
    assert_eq!(at("2026-10-19T12:01:30.000Z")["available"], true);
    assert_eq!(at("2026-10-19T12:01:30.001Z")["reason"], "stale");
    for r in [
        off,
        at("2026-10-20T00:00:00.000Z"),
        build_serving_live_response(Some(&t.0), ms(NOW)),
    ] {
        assert_eq!(validate::parse(&SERVING_LIVE_RESPONSE.node, &r).unwrap(), r);
    }
}

#[test]
fn missing_tier_and_file_selection() {
    let t = TempDir::new("serving-live-onegpu");
    t.write(
        "serving-feed-2026-10-18.jsonl",
        r#"{"schema":"serving-feed-v1","kind":"snapshot","tier":"gpu","ts":"2026-10-18T23:59:50.000Z","available":true,"models":[]}"#,
    );
    // Just after UTC midnight with nothing written today: yesterday's file, still fresh.
    let r = build_serving_live_response(Some(&t.0), ms("2026-10-19T00:00:10.000Z"));
    assert_eq!(r["file"], "serving-feed-2026-10-18.jsonl");
    assert_eq!(r["gpu"]["models"], json!([]));
    assert_eq!(
        r["onnx"],
        json!({"available": false, "sampled_at": null, "reason": "no snapshot in feed"})
    );
    assert_eq!(validate::parse(&SERVING_LIVE_RESPONSE.node, &r).unwrap(), r);
    let names: Vec<String> = [
        "serving-feed-2026-10-18.jsonl",
        "slots-2026-10-19.jsonl",
        "x",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    assert_eq!(
        select_serving_feed_file(&names, ms("2026-10-19T01:00:00Z")).as_deref(),
        Some("serving-feed-2026-10-18.jsonl")
    );
}
