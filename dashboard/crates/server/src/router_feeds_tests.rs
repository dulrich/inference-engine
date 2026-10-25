//! `router/tailReader.test.ts` byte-window edges, `router/feeds.test.ts` over the committed
//! fixtures, `currentFile` across UTC midnight, `routerStatus.test.ts` `parseStaleMsQuery`,
//! and the `summariseLegs` tie/null/even-median cases.
use super::*;
use crate::test_support::{fixtures, TempDir};
use serde_json::json;

const WINDOW: u64 = 1024 * 1024;

fn path(t: &TempDir, name: &str) -> String {
    jsrt::path_join(&t.0, name)
}

#[test]
fn tail_window_over_an_oversized_file() {
    let t = TempDir::new("tail-big");
    let filler = "x".repeat(900);
    let body: String = (0..3600)
        .map(|n| format!("{{\"n\":{n},\"filler\":\"{filler}\"}}\n"))
        .collect();
    t.write("big.jsonl", &body);
    let r = read_tail_lines(&path(&t, "big.jsonl"), WINDOW).unwrap();
    assert_eq!(r.size, body.len() as u64);
    assert!(r.truncated);
    let ns: Vec<i64> = r
        .lines
        .iter()
        .map(|l| jsrt::json_parse(l).unwrap()["n"].as_i64().unwrap())
        .collect();
    assert_eq!(*ns.last().unwrap(), 3599);
    assert!(ns[0] > 0);
    assert!(ns.windows(2).all(|w| w[1] == w[0] + 1));
}

#[test]
fn tail_window_edges() {
    let t = TempDir::new("tail-edges");
    t.write("small.jsonl", "{\"n\":0}\n{\"n\":1}\n{\"n\":2}\n");
    let r = read_tail_lines(&path(&t, "small.jsonl"), WINDOW).unwrap();
    assert!(!r.truncated);
    assert_eq!(r.lines, ["{\"n\":0}", "{\"n\":1}", "{\"n\":2}"]);
    t.write("empty.jsonl", "");
    assert_eq!(
        read_tail_lines(&path(&t, "empty.jsonl"), WINDOW).unwrap(),
        TailRead {
            lines: vec![],
            size: 0,
            window_bytes: WINDOW,
            truncated: false
        }
    );
    // Window exactly the file size: not truncated, the first line kept.
    let r = read_tail_lines(&path(&t, "small.jsonl"), 24).unwrap();
    assert_eq!((r.truncated, r.lines.len()), (false, 3));
    // One byte short: the straddled first line dropped.
    let r = read_tail_lines(&path(&t, "small.jsonl"), 23).unwrap();
    assert_eq!(
        (r.truncated, r.lines.clone()),
        (true, vec!["{\"n\":1}".to_string(), "{\"n\":2}".into()])
    );
    // No newline anywhere in a truncated window: nothing whole.
    t.write("nonl.jsonl", &format!("{{\"a\":1}}\n{}", "y".repeat(100)));
    assert!(read_tail_lines(&path(&t, "nonl.jsonl"), 50)
        .unwrap()
        .lines
        .is_empty());
    // A partial last line is kept for the parser to reject.
    t.write("partial.jsonl", "{\"n\":1}\n{\"n\":");
    let r = read_tail_lines(&path(&t, "partial.jsonl"), WINDOW).unwrap();
    assert_eq!(r.lines, ["{\"n\":1}", "{\"n\":"]);
    let (records, skipped) = parse_jsonl_lines(&r.lines, &ROUTER_SIDECAR_RECORD);
    assert_eq!((records.len(), skipped), (0, 2));
    // An unstattable path reads as size 0 (Bun.file(...).size), never an error.
    assert!(read_tail_lines(&path(&t, "absent.jsonl"), WINDOW)
        .unwrap()
        .lines
        .is_empty());
    assert_eq!(DEFAULT_TAIL_WINDOW_BYTES, 1024 * 1024);
}

fn ms(iso: &str) -> f64 {
    jsrt::date_parse(iso).unwrap()
}

fn names(v: &[&str]) -> Vec<String> {
    v.iter().map(|s| s.to_string()).collect()
}

#[test]
fn current_file_selection() {
    let n = names(&[
        "router-2026-09-13.2.jsonl",
        "router-2026-09-13.10.jsonl",
        "router-2026-09-12.99.jsonl",
        "notes.txt",
        "router-today.jsonl",
    ]);
    assert_eq!(
        select_router_telemetry_file(&n, ms("2026-09-13T04:00:00Z")).as_deref(),
        Some("router-2026-09-13.10.jsonl")
    );
    // Just after UTC midnight with nothing written today: the newest day present.
    assert_eq!(
        select_router_telemetry_file(&n, ms("2026-09-14T00:00:01Z")).as_deref(),
        Some("router-2026-09-13.10.jsonl")
    );
    // Just before midnight on the 12th: the 12th's file is today's.
    assert_eq!(
        select_router_telemetry_file(&n, ms("2026-09-12T23:59:59.999Z")).as_deref(),
        Some("router-2026-09-12.99.jsonl")
    );
    let d = names(&["codex-sidecar-2026-09-12.jsonl"]);
    assert_eq!(
        select_sidecar_file(&d, ms("2026-09-13T00:00:00Z")).as_deref(),
        Some("codex-sidecar-2026-09-12.jsonl")
    );
    assert_eq!(select_sidecar_file(&names(&["x"]), 0.0), None);
}

#[test]
fn stale_ms_query() {
    assert_eq!(parse_stale_ms_query(Some("5000")), Some(5000.0));
    assert_eq!(parse_stale_ms_query(Some("1000")), Some(STALE_MS_MIN));
    assert_eq!(parse_stale_ms_query(Some("600000")), Some(STALE_MS_MAX));
    for bad in [
        None,
        Some("soon"),
        Some("999"),
        Some("600001"),
        Some("NaN"),
        Some("Infinity"),
        Some(""),
    ] {
        assert_eq!(parse_stale_ms_query(bad), None, "{bad:?}");
    }
}

const NOW: &str = "2026-09-13T04:00:10.000Z";

fn logs() -> String {
    fixtures("router/logs")
}

#[test]
fn telemetry_over_fixtures() {
    let r = build_router_telemetry_response(Some(&logs()), None, ms(NOW));
    assert_eq!(r["file"], "router-2026-09-13.10.jsonl");
    assert_eq!(r["windowBytes"], 1_048_576);
    let legs = r["legs"].as_array().unwrap();
    let leg = |l: Value| legs.iter().find(|x| x["leg"] == l).unwrap().clone();
    assert_eq!(
        leg(json!("anthropic")),
        json!({"leg": "anthropic", "requests": 2.0, "errors": 1.0, "p50TtftMs": 1200.0})
    );
    assert_eq!(
        leg(json!("claude-local")),
        json!({"leg": "claude-local", "requests": 2.0, "errors": 0.0, "p50TtftMs": 630.0})
    );
    assert_eq!(
        leg(Value::Null),
        json!({"leg": null, "requests": 1.0, "errors": 1.0, "p50TtftMs": null})
    );
    let inflight = r["inFlight"].as_array().unwrap();
    assert_eq!(inflight.len(), 1);
    assert_eq!(inflight[0]["reqId"], "req-inflight-live");
    assert_eq!(inflight[0]["tokPerSec"], 50);
    let wide = build_router_telemetry_response(Some(&logs()), Some(600_000.0), ms(NOW));
    let abandoned = wide["inFlight"]
        .as_array()
        .unwrap()
        .iter()
        .find(|x| x["reqId"] == "req-inflight-abandoned")
        .unwrap()
        .clone();
    assert_eq!(abandoned["tokPerSec"], Value::Null);
    let un = |reason: &str| json!({"available": false, "reason": reason});
    assert_eq!(
        build_router_telemetry_response(None, None, 0.0),
        un("disabled")
    );
    assert_eq!(
        build_router_telemetry_response(Some("/nonexistent-bd"), None, 0.0),
        un("unreadable")
    );
    let empty = TempDir::new("router-empty");
    assert_eq!(
        build_router_telemetry_response(Some(&empty.0), None, 0.0),
        un("empty")
    );
}

fn req(leg: Option<&str>, status: f64, ttft: Option<f64>) -> Value {
    json!({"leg": leg, "status": status, "error": null, "ttftMs": ttft})
}

#[test]
fn summarise_legs_ties_null_and_even_median() {
    let recent = vec![
        req(Some("b"), 200.0, Some(10.0)),
        req(None, 500.0, None),
        req(Some("a"), 200.0, Some(30.0)),
        req(Some("B"), 200.0, Some(1.0)),
        req(Some("a"), 200.0, Some(10.0)),
        req(Some("b"), 200.0, Some(20.0)),
        json!({"leg": "c", "status": null, "ttftMs": null}),
    ];
    let legs = summarise_legs(&recent);
    let order: Vec<Option<&str>> = legs.iter().map(|l| l.leg.as_deref()).collect();
    // Two-request ties by localeCompare; the one-request ties: "" (null) < "B" < "c".
    assert_eq!(order, [Some("a"), Some("b"), None, Some("B"), Some("c")]);
    assert_eq!(legs[0].p50_ttft_ms, Some(20.0));
    assert_eq!(legs[1].p50_ttft_ms, Some(15.0));
    assert_eq!((legs[2].errors, legs[2].p50_ttft_ms), (1.0, None));
    // An absent `error` is `undefined !== null`: counted as an error.
    assert_eq!(legs[4].errors, 1.0);
}

#[test]
fn sidecar_over_fixtures() {
    let r = build_router_sidecar_response(Some(&fixtures("router/sidecar")), ms(NOW));
    assert_eq!(r["available"], true);
    let personas: Vec<&str> = r["personas"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p["persona"].as_str().unwrap())
        .collect();
    let mut sorted = personas.clone();
    sorted.sort_by(|a, b| jsrt::locale_compare(a, b));
    assert_eq!(personas, sorted);
    let errors = r["recentErrors"].as_array().unwrap();
    assert!(errors
        .iter()
        .all(|e| SIDECAR_ERROR_KINDS.contains(&e["kind"].as_str().unwrap())));
}

#[test]
fn personas_and_recent_errors() {
    let records = vec![
        json!({"ts": "1", "kind": "config-verified", "persona": "zed", "threadId": "t1", "model": "m1"}),
        json!({"ts": "2", "kind": "count_tokens", "persona": "zed", "threadId": null}),
        json!({"ts": "3", "kind": "child-stderr", "persona": "Amy", "text": "boom"}),
        json!({"ts": "4", "kind": "http-error", "persona": "amy", "status": 502, "message": "bad", "text": "ignored"}),
        json!({"ts": "5", "kind": "orphan-tool-result", "persona": "amy"}),
    ];
    let p = summarise_personas(&records);
    assert_eq!(
        p[0],
        json!({"persona": "amy", "threadId": null, "model": null, "lastSeenTs": "5", "lastKind": "orphan-tool-result"})
    );
    assert_eq!(p[1]["persona"], "Amy");
    assert_eq!(
        p[2],
        json!({"persona": "zed", "threadId": "t1", "model": "m1", "lastSeenTs": "2", "lastKind": "count_tokens"})
    );
    let e = collect_recent_errors(&records, 2);
    assert_eq!(
        e[0],
        json!({"ts": "4", "persona": "amy", "kind": "http-error", "status": 502, "message": "bad"})
    );
    assert_eq!(
        e[1],
        json!({"ts": "5", "persona": "amy", "kind": "orphan-tool-result", "status": null, "message": null})
    );
    assert_eq!(collect_recent_errors(&records, 20)[0]["message"], "boom");
}
