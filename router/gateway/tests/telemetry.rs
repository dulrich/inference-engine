//! 0006-parity tests for the telemetry sink (src/telemetry/). Temp dirs only.

use axum::http::{HeaderMap, HeaderValue};
use inference_router::relay::Route;
use inference_router::telemetry::sink::{self, iso8601, utc_day, Clock};
use inference_router::telemetry::{
    read_session_id, read_subagent_witness, JsonlTelemetry, Outcome, OutcomeKind, RequestStart,
    RequestTelemetry, Resolution, Sink, SinkOptions, Telemetry,
};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const BEARER: &str = "sk-ant-oat01-TESTBEARER-DO-NOT-LOG";
const GATEWAY_KEY: &str = "GATEWAYKEY-TESTVALUE-DO-NOT-LOG";
const MESSAGE_TEXT: &str = "the capital of assyria is a secret prompt string";
const SYSTEM_TEXT: &str = "You are Claude Code. cc_is_subagent=true; billing line";
const SESSION_ID: &str = "7f3c1e88-9d21-4a6b-bb0f-2c5d9e410000";

struct TempDir(PathBuf);
impl TempDir {
    fn new() -> TempDir {
        static N: AtomicU64 = AtomicU64::new(0);
        let p = std::env::temp_dir().join(format!(
            "router-telemetry-test-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        TempDir(p)
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn opts(dir: &Path, max_bytes: u64, keep_files: usize) -> SinkOptions {
    SinkOptions {
        dir: dir.to_path_buf(),
        max_bytes,
        keep_files,
    }
}

fn files(dir: &Path) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    v.sort();
    v
}

fn raw(dir: &Path) -> String {
    files(dir)
        .iter()
        .map(|n| std::fs::read_to_string(dir.join(n)).unwrap())
        .collect()
}

fn lines(dir: &Path) -> Vec<Value> {
    raw(dir)
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn body() -> Value {
    json!({
        "model": "claude-local/qwen36-a3b",
        "stream": true,
        "metadata": {"user_id": format!("user_abc123hash_account_11112222-3333-4444-5555-666677778888_session_{SESSION_ID}")},
        "system": [{"type": "text", "text": SYSTEM_TEXT}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": MESSAGE_TEXT}]}]
    })
}

fn headers() -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert(
        "authorization",
        HeaderValue::from_str(&format!("Bearer {BEARER}")).unwrap(),
    );
    h.insert("x-gateway-key", HeaderValue::from_static(GATEWAY_KEY));
    h.insert("x-api-key", HeaderValue::from_static(GATEWAY_KEY));
    h
}

fn start(t: &JsonlTelemetry, b: &Value, streaming: bool) -> Box<dyn RequestTelemetry> {
    let h = headers();
    t.request_start(&RequestStart {
        request_id: "req-telemetry-1",
        route: Route::Messages,
        requested_model: b.get("model").and_then(Value::as_str),
        headers: &h,
        body: Some(b),
        streaming,
        started_at: Instant::now(),
        started_wall: SystemTime::now(),
    })
}

fn resolve(r: &mut dyn RequestTelemetry, attempt: usize) {
    r.resolved(&Resolution {
        provider_name: "claude-local",
        provider_kind: "openai",
        model: "qwen36-a3b",
        upstream_url: "http://inference-engine-llama:4806/v1/messages",
        attempt,
    });
}

fn outcome(kind: OutcomeKind, status: u16, error: Option<(&str, &str)>) -> Outcome {
    Outcome {
        kind,
        status,
        error: error.map(|(s, m)| (s.to_string(), m.to_string())),
        elapsed: Duration::from_millis(1234),
        upstream_headers_after: Some(Duration::from_millis(10)),
    }
}

fn setup() -> (TempDir, Arc<Sink>, JsonlTelemetry) {
    let d = TempDir::new();
    let s = Sink::new(opts(&d.0, 1 << 20, 14), sink::system_clock());
    let t = JsonlTelemetry::new(s.clone());
    (d, s, t)
}

const ANTHROPIC_SSE: [&str; 4] = [
    "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"model\":\"claude-sonnet-4-6\",\"usage\":{\"input_tokens\":32,\"cache_read_input_tokens\":189434}}}\n\n",
    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"thinking_delta\",\"thinking\":\"hmm\"}}\n\n",
    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"hello\"}}\n\n",
    "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":1865}}\n\n",
];

#[tokio::test]
async fn terminal_record_carries_the_pinned_fields() {
    let (d, s, t) = setup();
    let mut r = start(&t, &body(), true);
    resolve(r.as_mut(), 0);
    r.upstream_chunk(
        b"data: {\"model\":\"qwen36-a3b\",\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n",
    );
    r.upstream_chunk(b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":17,\"completion_tokens\":40,\"prompt_tokens_details\":{\"cached_tokens\":13}},\"timings\":{\"prompt_n\":17}}\n\ndata: [DONE]\n\n");
    r.finalize(&outcome(OutcomeKind::Completed, 200, None));
    s.flush();
    let l = lines(&d.0);
    assert_eq!(l.len(), 1);
    let rec = &l[0];
    let expect = json!({
        "kind": "request", "reqId": "req-telemetry-1", "route": "/v1/messages", "method": "POST",
        "sourceAdapterKey": "anthropic_messages", "leg": "claude-local", "providerType": "openai",
        "upstreamUrl": "http://inference-engine-llama:4806/v1/messages", "sessionId": SESSION_ID,
        "isSubagent": true, "modelRequested": "claude-local/qwen36-a3b", "modelResolved": "qwen36-a3b",
        "stream": true, "status": 200, "attemptSequence": 1, "stopReason": "stop", "error": null,
        "upstreamTimings": null, "wallMs": 1234, "responseTokens": 1, "reasoningTokens": 0,
        "usage": {"input": 17, "output": 40, "cacheRead": 13, "cacheWrite": null}
    });
    for (k, v) in expect.as_object().unwrap() {
        assert_eq!(&rec[k], v, "field {k}");
    }
    assert!(rec["ttftMs"].is_u64() && rec["upstreamFirstByteMs"].is_u64());
    assert!(rec["ts"].as_str().unwrap().ends_with('Z'));
    assert_eq!(rec.as_object().unwrap().len(), 25, "0006 has 25 keys");
}

#[tokio::test]
async fn never_writes_bearer_gateway_key_system_or_message_content() {
    let (d, s, t) = setup();
    let mut r = start(&t, &body(), true);
    resolve(r.as_mut(), 0);
    r.upstream_chunk(
        format!("data: {{\"choices\":[{{\"delta\":{{\"content\":\"{MESSAGE_TEXT}\"}}}}]}}\n\n")
            .as_bytes(),
    );
    r.finalize(&outcome(OutcomeKind::Completed, 200, None));
    s.flush();
    let text = raw(&d.0);
    assert!(!text.is_empty());
    for bad in [
        BEARER,
        GATEWAY_KEY,
        "Bearer ",
        MESSAGE_TEXT,
        SYSTEM_TEXT,
        "authorization",
        "x-gateway-key",
        "x-api-key",
        "user_abc123hash_account_",
    ] {
        assert!(!text.contains(bad), "leaked {bad}");
    }
    assert!(text.contains(SESSION_ID));
}

#[tokio::test]
async fn records_the_failing_stage() {
    let (d, s, t) = setup();
    let r = start(&t, &body(), false);
    r.finalize(&outcome(
        OutcomeKind::Refused,
        400,
        Some(("model_resolution", "Model is not allowed for provider.")),
    ));
    s.flush();
    let rec = &lines(&d.0)[0];
    assert_eq!(rec["status"], 400);
    assert_eq!(rec["leg"], Value::Null);
    assert_eq!(
        rec["error"],
        json!({"stage": "model_resolution", "message": "Model is not allowed for provider."})
    );
}

#[tokio::test]
async fn finalize_once_on_upstream_error_and_fallthrough_counts_attempts() {
    let (d, s, t) = setup();
    let mut r = start(&t, &body(), false);
    resolve(r.as_mut(), 0);
    resolve(r.as_mut(), 1);
    r.upstream_chunk(br#"{"type":"error","error":{"type":"overloaded_error","message":"x"}}"#);
    r.finalize(&outcome(
        OutcomeKind::UpstreamError,
        529,
        Some(("upstream_status", "529")),
    ));
    s.flush();
    let l = lines(&d.0);
    assert_eq!(l.len(), 1);
    assert_eq!(l[0]["attemptSequence"], 2);
    assert_eq!(l[0]["status"], 529);
}

#[tokio::test]
async fn client_disconnect_writes_one_499_record_and_stops_progress() {
    let (d, s, t) = setup();
    let r = start(&t, &body(), true);
    r.finalize(&outcome(OutcomeKind::ClientDisconnect, 499, None));
    tokio::time::sleep(Duration::from_millis(1300)).await;
    s.flush();
    let l = lines(&d.0);
    assert_eq!(l.len(), 1, "{l:?}");
    assert_eq!(l[0]["kind"], "request");
    assert_eq!(l[0]["status"], 499);
}

#[tokio::test(start_paused = true)]
async fn progress_cadence_from_request_start_and_terminal_last() {
    let (d, s, t) = setup();
    let mut r = start(&t, &body(), true);
    resolve(r.as_mut(), 0);
    // No upstream bytes yet: the cadence still runs (prefill).
    tokio::time::sleep(Duration::from_millis(3500)).await;
    r.upstream_chunk(b"data: {\"choices\":[{\"delta\":{\"content\":\"a\"}}]}\n\n");
    tokio::time::sleep(Duration::from_millis(1000)).await;
    r.finalize(&outcome(OutcomeKind::Completed, 200, None));
    tokio::time::sleep(Duration::from_millis(5000)).await;
    s.flush();
    let l = lines(&d.0);
    let progress: Vec<&Value> = l.iter().filter(|x| x["kind"] == "progress").collect();
    assert_eq!(progress.len(), 4, "{l:?}");
    assert_eq!(l.last().unwrap()["kind"], "request");
    assert_eq!(l.iter().filter(|x| x["kind"] == "request").count(), 1);
    assert_eq!(progress[0]["phase"], "prefill");
    assert_eq!(progress[0]["sessionId"], SESSION_ID);
    assert_eq!(progress[0]["leg"], "claude-local");
    assert_eq!(progress[0]["responseTokens"], 0);
    assert_eq!(progress[0]["elapsedMs"], 1000);
    assert_eq!(progress[3]["phase"], "decode");
    assert_eq!(progress[3]["responseTokens"], 1);
    assert_eq!(progress[3]["elapsedMs"], 4000);
}

#[tokio::test(start_paused = true)]
async fn non_streaming_requests_emit_no_progress() {
    let (d, s, t) = setup();
    let r = start(&t, &body(), false);
    tokio::time::sleep(Duration::from_millis(3500)).await;
    r.finalize(&outcome(OutcomeKind::Completed, 200, None));
    s.flush();
    assert_eq!(lines(&d.0).len(), 1);
}

#[tokio::test]
async fn usage_from_anthropic_sse_split_across_chunks() {
    let (d, s, t) = setup();
    let mut r = start(&t, &body(), true);
    resolve(r.as_mut(), 0);
    let all = ANTHROPIC_SSE.concat();
    // Byte-split mid-line, including inside a multi-event chunk.
    for piece in all.as_bytes().chunks(37) {
        r.upstream_chunk(piece);
    }
    r.finalize(&outcome(OutcomeKind::Completed, 200, None));
    s.flush();
    let rec = &lines(&d.0)[0];
    assert_eq!(rec["modelResolved"], "claude-sonnet-4-6");
    assert_eq!(rec["stopReason"], "end_turn");
    assert_eq!(rec["responseTokens"], 1);
    assert_eq!(rec["reasoningTokens"], 1);
    assert_eq!(
        rec["usage"],
        json!({"input": 32, "output": 1865, "cacheRead": 189434, "cacheWrite": null})
    );
    assert!(rec["ttftMs"].is_u64());
}

#[tokio::test]
async fn usage_from_buffered_anthropic_json() {
    let (d, s, t) = setup();
    let mut r = start(&t, &body(), false);
    resolve(r.as_mut(), 0);
    let b = br#"{"id":"msg_1","type":"message","model":"claude-opus-4-6","content":[{"type":"text","text":"secret answer"}],"stop_reason":"max_tokens","usage":{"input_tokens":5,"output_tokens":7,"cache_creation_input_tokens":3}}"#;
    r.upstream_chunk(&b[..20]);
    r.upstream_chunk(&b[20..]);
    r.finalize(&outcome(OutcomeKind::Completed, 200, None));
    s.flush();
    let rec = &lines(&d.0)[0];
    assert_eq!(rec["modelResolved"], "claude-opus-4-6");
    assert_eq!(rec["stopReason"], "max_tokens");
    assert_eq!(rec["ttftMs"], Value::Null);
    assert_eq!(
        rec["usage"],
        json!({"input": 5, "output": 7, "cacheRead": null, "cacheWrite": 3})
    );
    assert!(!raw(&d.0).contains("secret answer"));
}

#[tokio::test]
async fn usage_from_buffered_openai_json() {
    let (d, s, t) = setup();
    let mut r = start(&t, &body(), false);
    r.upstream_chunk(br#"{"model":"gpt-x","choices":[{"message":{"content":"hi"},"finish_reason":"length"}],"usage":{"prompt_tokens":9,"completion_tokens":2}}"#);
    r.finalize(&outcome(OutcomeKind::Completed, 200, None));
    s.flush();
    let rec = &lines(&d.0)[0];
    assert_eq!(rec["modelResolved"], "gpt-x");
    assert_eq!(rec["stopReason"], "length");
    assert_eq!(rec["usage"]["input"], 9.0);
    assert_eq!(rec["usage"]["output"], 2.0);
}

#[test]
fn rotates_by_size_and_keeps_at_most_keep_files() {
    let d = TempDir::new();
    let s = Sink::new(opts(&d.0, 1024, 2), sink::system_clock());
    for i in 0..40 {
        s.write(
            &json!({"kind": "request", "reqId": format!("req-{i}"), "filler": "x".repeat(200)}),
        );
        s.flush();
    }
    let f = files(&d.0);
    assert_eq!(f.len(), 2, "{f:?}");
    for n in &f {
        assert!(n.starts_with("router-") && n.ends_with(".jsonl"));
        assert!(std::fs::metadata(d.0.join(n)).unwrap().len() <= 1024 + 256);
    }
    let l = lines(&d.0);
    assert!(l.last().unwrap()["reqId"] == "req-39");
}

#[test]
fn retention_orders_by_numeric_index() {
    let d = TempDir::new();
    let s = Sink::new(opts(&d.0, 1024, 3), sink::system_clock());
    for i in 0..60 {
        s.write(&json!({"reqId": format!("req-{i}"), "filler": "x".repeat(300)}));
        s.flush();
    }
    let day = utc_day(SystemTime::now());
    let mut idx: Vec<u64> = files(&d.0)
        .iter()
        .map(|n| {
            n.strip_prefix(&format!("router-{day}."))
                .unwrap()
                .strip_suffix(".jsonl")
                .unwrap()
                .parse()
                .unwrap()
        })
        .collect();
    idx.sort();
    assert_eq!(idx.len(), 3);
    assert!(idx[0] >= 10, "kept newest indices, got {idx:?}");
    assert_eq!(idx[2] - idx[0], 2);
}

#[test]
fn rotates_on_utc_day_change() {
    let d = TempDir::new();
    let now = Arc::new(AtomicU64::new(1_760_313_599)); // 2025-10-12T23:59:59Z
    let n2 = now.clone();
    let clock: Clock =
        Arc::new(move || UNIX_EPOCH + Duration::from_secs(n2.load(Ordering::Relaxed)));
    let s = Sink::new(opts(&d.0, 1 << 20, 14), clock);
    s.write(&json!({"reqId": "a"}));
    s.flush();
    now.store(1_760_313_601, Ordering::Relaxed);
    s.write(&json!({"reqId": "b"}));
    s.flush();
    assert_eq!(
        files(&d.0),
        vec!["router-2025-10-12.0.jsonl", "router-2025-10-13.0.jsonl"]
    );
}

#[test]
fn resumes_highest_index_for_the_day() {
    let d = TempDir::new();
    let day = utc_day(SystemTime::now());
    std::fs::write(d.0.join(format!("router-{day}.7.jsonl")), "{\"old\":1}\n").unwrap();
    let s = Sink::new(opts(&d.0, 1 << 20, 14), sink::system_clock());
    s.write(&json!({"new": 1}));
    s.flush();
    assert_eq!(files(&d.0), vec![format!("router-{day}.7.jsonl")]);
    assert_eq!(lines(&d.0).len(), 2);
}

#[test]
fn flushes_whole_lines_only() {
    let d = TempDir::new();
    let s = Sink::new(opts(&d.0, 1 << 24, 14), sink::system_clock());
    for i in 0..500 {
        s.write(
            &json!({"kind": "progress", "reqId": format!("req-{i}"), "filler": "y".repeat(300)}),
        );
    }
    s.flush();
    let text = raw(&d.0);
    assert!(text.ends_with('\n'));
    assert_eq!(lines(&d.0).len(), 500);
}

#[test]
fn drops_lines_over_the_64k_cap() {
    let d = TempDir::new();
    let s = Sink::new(opts(&d.0, 1 << 24, 14), sink::system_clock());
    s.write(&json!({"big": "z".repeat(70 * 1024)}));
    s.write(&json!({"small": 1}));
    s.flush();
    assert_eq!(lines(&d.0), vec![json!({"small": 1})]);
}

#[tokio::test]
async fn unwritable_dir_disables_fail_open() {
    let d = TempDir::new();
    let blocker = d.0.join("not-a-directory");
    std::fs::write(&blocker, "x").unwrap();
    let s = Sink::new(opts(&blocker.join("logs"), 1024, 2), sink::system_clock());
    let t = JsonlTelemetry::new(s.clone());
    let r = start(&t, &body(), true);
    r.finalize(&outcome(OutcomeKind::Completed, 200, None));
    s.flush();
    assert!(s.is_disabled());
    s.write(&json!({"k": 1}));
    s.flush();
    assert_eq!(files(&d.0), vec!["not-a-directory"]);
}

#[test]
fn env_knobs_and_defaults() {
    let none = |_: &str| None;
    let (en, o) = sink::options_from_env(&none);
    assert!(en);
    assert_eq!(
        o,
        opts(Path::new("/var/log/inference-router"), 16 * 1024 * 1024, 14)
    );
    let set = |k: &str| {
        match k {
            "ROUTER_TELEMETRY_ENABLED" => Some("false"),
            "ROUTER_TELEMETRY_DIR" => Some("/tmp/sink"),
            "ROUTER_TELEMETRY_MAX_BYTES" => Some("2048"),
            "ROUTER_TELEMETRY_KEEP_FILES" => Some("3"),
            _ => None,
        }
        .map(str::to_string)
    };
    let (en, o) = sink::options_from_env(&set);
    assert!(!en);
    assert_eq!(o, opts(Path::new("/tmp/sink"), 2048, 3));
    let bad = |k: &str| (k == "ROUTER_TELEMETRY_MAX_BYTES").then(|| "-5".to_string());
    assert_eq!(sink::options_from_env(&bad).1.max_bytes, 16 * 1024 * 1024);
}

#[test]
fn session_id_and_subagent_witness() {
    assert_eq!(read_session_id(&body()).as_deref(), Some(SESSION_ID));
    assert_eq!(read_session_id(&json!({})), None);
    assert_eq!(
        read_session_id(&json!({"metadata": {"user_id": "user_nosession"}})),
        None
    );
    assert_eq!(
        read_session_id(&json!({"metadata": {"user_id": "u_session_short"}})),
        None
    );
    assert!(read_subagent_witness(&body()));
    assert!(read_subagent_witness(
        &json!({"system": "x cc_is_subagent=true"})
    ));
    assert!(!read_subagent_witness(
        &json!({"system": [{"type": "text", "text": "no witness"}]})
    ));
    assert!(!read_subagent_witness(&json!({})));
}

#[test]
fn systemone_body_is_never_read() {
    let d = TempDir::new();
    let s = Sink::new(opts(&d.0, 1 << 20, 14), sink::system_clock());
    let t = JsonlTelemetry::new(s.clone());
    let h = headers();
    let r = t.request_start(&RequestStart {
        request_id: "r1",
        route: Route::Systemone,
        requested_model: None,
        headers: &h,
        body: None,
        streaming: false,
        started_at: Instant::now(),
        started_wall: SystemTime::now(),
    });
    r.finalize(&outcome(OutcomeKind::Completed, 200, None));
    s.flush();
    let rec = &lines(&d.0)[0];
    assert_eq!(rec["route"], "/v1/systemone");
    assert_eq!(rec["sourceAdapterKey"], "systemone");
    assert_eq!(rec["sessionId"], Value::Null);
}

#[test]
fn utc_formatting() {
    let t = UNIX_EPOCH + Duration::from_millis(951_782_400_123); // 2000-02-29
    assert_eq!(utc_day(t), "2000-02-29");
    assert_eq!(iso8601(t), "2000-02-29T00:00:00.123Z");
    let t = UNIX_EPOCH + Duration::from_secs(1_760_313_599);
    assert_eq!(iso8601(t), "2025-10-12T23:59:59.000Z");
}
