//! Port of sidecar/test/unit.test.mjs, case for case. The `wireModelId` assertions live in
//! `sinks.mjs` (WP-3 scope) and are ported with that module, not here.

use codex_sidecar::codex::{CallId, ThreadId, TokenUsage, ToolCall, Usage};
use codex_sidecar::errors::{map_upstream_error, rate_limit_exhaustion, retry_after_seconds};
use codex_sidecar::logging::Logger;
use codex_sidecar::protocol::*;
use codex_sidecar::request::{normalize_request, Message, Request};

fn req(v: Value) -> Request {
    Request::parse(v).unwrap()
}

fn msgs(v: Vec<Value>) -> Vec<Message> {
    serde_json::from_value(Value::Array(v)).unwrap()
}
use codex_sidecar::thread_config::{verify_features, verify_instruction_sources};
use serde_json::{json, Map, Value};
use std::os::unix::fs::PermissionsExt;

const TITLE_SYSTEM: &str =
    "You are naming a coding session so the user can pick it out of a long list of sessions.";

fn pm(id: Option<Value>) -> Option<(String, String, &'static str)> {
    let text = Request::parse(json!({ "model": id })).unwrap().model_text();
    parse_model_id(&text).map(|p| (p.model, p.effort, p.effort_source.as_str()))
}

fn want(m: &str, e: &str, s: &'static str) -> Option<(String, String, &'static str)> {
    Some((m.into(), e.into(), s))
}

#[test]
fn effort_lives_in_the_model_id_suffix() {
    let cases = [
        ("gpt-5.6-luna", "gpt-5.6-luna", "low", "default"),
        ("gpt-5.6-sol", "gpt-5.6-sol", "medium", "default"),
        ("gpt-6-astra", "gpt-6-astra", "medium", "default"),
        ("gpt-6-astra-low", "gpt-6-astra", "low", "suffix"),
        ("gpt-5.6-luna-high", "gpt-5.6-luna", "high", "suffix"),
        ("gpt-5.6-sol-low", "gpt-5.6-sol", "low", "suffix"),
        ("gpt-6-sol", "gpt-6-sol", "medium", "default"),
        ("gpt-6-luna", "gpt-6-luna", "low", "default"),
        ("gpt-6-sol-low", "gpt-6-sol", "low", "suffix"),
        ("gpt-6.1-sol", "gpt-6.1-sol", "medium", "default"),
        ("gpt-6.1-sol-low", "gpt-6.1-sol", "low", "suffix"),
        (
            "codex-b/gpt-6.1-sol-high",
            "gpt-6.1-sol",
            "high",
            "suffix",
        ),
        ("gpt-6-luna-high", "gpt-6-luna", "high", "suffix"),
        (
            "codex-a/gpt-5.6-luna-medium",
            "gpt-5.6-luna",
            "medium",
            "suffix",
        ),
    ];
    for (id, m, e, s) in cases {
        assert_eq!(pm(Some(json!(id))), want(m, e, s), "{id}");
    }
    assert_eq!(pm(Some(json!("gpt-5.6-luna-ultra"))), None);
    assert_eq!(pm(Some(json!("gpt-7-nova"))), None);
    assert_eq!(pm(Some(json!(""))), None);
    assert_eq!(pm(None), None);
}

#[test]
fn title_signature_needs_three_robust_conditions() {
    let session_msg = msgs(vec![
        json!({"role": "user", "content": "<session>chat</session>"}),
    ]);
    let cc2269 = msgs(vec![
        json!({"role": "user", "content": "<session>chat</session>\n\nWrite the title in the predominant language of the session — a stray word or code token in another language doesn't change it, and neither does the English of these instructions."}),
    ]);
    let drifted = msgs(vec![
        json!({"role": "user", "content": "please read <session>x</session> for me"}),
    ]);
    let empty_tools = req(json!({"tools": []}));
    assert!(
        is_title_request(&empty_tools, TITLE_SYSTEM, &cc2269),
        "2.1.269 envelope"
    );
    assert!(
        is_title_request(&empty_tools, TITLE_SYSTEM, &drifted),
        "envelope drift is fail-safe"
    );
    assert_eq!(title_envelope(&session_msg), "pinned");
    assert_eq!(title_envelope(&cc2269), "pinned");
    assert_eq!(title_envelope(&drifted), "drifted");
    assert!(is_title_request(&empty_tools, TITLE_SYSTEM, &session_msg));
    assert!(
        is_title_request(&req(json!({})), TITLE_SYSTEM, &session_msg),
        "absent tools count as empty"
    );
    assert!(
        !is_title_request(
            &req(json!({"tools": [{"name": "Bash"}]})),
            TITLE_SYSTEM,
            &session_msg
        ),
        "tools present"
    );
    assert!(
        !is_title_request(&empty_tools, "Summarise this chat", &session_msg),
        "phrase missing"
    );
    let long = format!("{TITLE_SYSTEM}{}", "x".repeat(4000));
    assert!(
        !is_title_request(&empty_tools, &long, &session_msg),
        "system too long"
    );
    let two = vec![
        session_msg[0].clone(),
        msgs(vec![json!({"role": "assistant", "content": "Title"})]).remove(0),
    ];
    assert!(
        !is_title_request(&empty_tools, TITLE_SYSTEM, &two),
        "more than one message"
    );
}

#[test]
fn cwd_comes_from_environment_block_and_system_messages_drop() {
    let msgs = json!([
        {"role": "system", "content": [{"type": "text", "text": "Here is useful environment information:\nPrimary working directory: /home/u/repo\nIs a git repo: yes"}]},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "system", "content": "<system-reminder>budget</system-reminder>"},
    ]);
    let norm = normalize_request(req(json!({ "messages": msgs })), None);
    assert_eq!(norm.raw_cwd.as_deref(), Some("/home/u/repo"));
    assert_eq!(norm.msgs.len(), 2, "system-role entries are not turns");
    assert_eq!(
        extract_cwd(
            &self::msgs(vec![json!({"role": "user", "content": "no env here"})]),
            ""
        ),
        None
    );
    assert_eq!(
        extract_cwd(&[], "Primary working directory: /srv/x").as_deref(),
        Some("/srv/x")
    );
}

#[test]
fn sessions_key_on_metadata_user_id_and_fall_back_to_a_warned_hash() {
    let body = |sid: &str| json!({"metadata": {"user_id": json!({"device_id": "d", "account_uuid": "a", "session_id": sid}).to_string()}});
    assert_eq!(
        session_key_of(&req(body("abc-123")), "sys", "hi", None),
        SessionKey {
            key: "abc-123".into(),
            weak: false,
            scope: Scope::Parent
        }
    );
    let a = session_key_of(&req(json!({})), "sys", "hi", None);
    let b = session_key_of(&req(json!({})), "sys", "hi", None);
    let c = session_key_of(&req(json!({})), "sys", "different", None);
    assert!(a.weak);
    assert_eq!(a.key, b.key, "the fallback is stable for the same prompt");
    assert_ne!(a.key, c.key);
    assert!(
        session_key_of(
            &req(json!({"metadata": {"user_id": "not json"}})),
            "sys",
            "hi",
            None
        )
        .weak
    );
}

#[test]
fn upstream_errors_rate_limits_and_instruction_sources_map_as_schema_says() {
    let m = |v: Value| map_upstream_error(&v, "app-server");
    assert_eq!(m(json!({"message": "boom"})).status, 502);
    assert_eq!(
        m(json!({"message": "nope", "data": {"codexErrorInfo": "unauthorized"}})).status,
        401
    );
    assert_eq!(
        m(json!({"message": "nope", "codexErrorInfo": "rateLimitExceeded"})).status,
        429
    );
    assert_eq!(
        m(json!({"message": "x", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}})).status,
        429
    );
    assert_eq!(
        m(json!({"message": "usage limit reached for this account"})).anthropic_type,
        "rate_limit_error"
    );

    let now = 1_700_000_000_000.0;
    assert_eq!(
        retry_after_seconds(Some(&json!(now / 1000.0 + 60.0)), now),
        Some(60.0)
    );
    assert_eq!(
        retry_after_seconds(Some(&json!(now + 60_000.0)), now),
        Some(60.0)
    );
    assert_eq!(
        retry_after_seconds(Some(&json!(now - 5000.0)), now),
        Some(1.0),
        "a past reset still asks for one second"
    );
    assert_eq!(retry_after_seconds(Some(&Value::Null), now), None);

    let rl = |v: Value| rate_limit_exhaustion(Some(&v), now);
    assert_eq!(rl(json!({"primary": {"usedPercent": 40}})), None);
    assert_eq!(
        rl(json!({"primary": {"usedPercent": 100}})).unwrap().reason,
        json!("rate_limit_reached")
    );
    assert_eq!(
        rl(json!({"secondary": {"usedPercent": 10}, "rateLimitReachedType": "workspace_owner_credits_depleted"}))
            .unwrap()
            .reason,
        json!("workspace_owner_credits_depleted")
    );

    assert_eq!(verify_instruction_sources(Some(&json!([]))), None);
    assert_eq!(
        verify_instruction_sources(Some(&json!(["/home/u/.codex-a/AGENTS.md"]))),
        None
    );
    assert!(
        verify_instruction_sources(Some(&json!(["/a/AGENTS.md", "/b/AGENTS.md"])))
            .unwrap()
            .contains("at most")
    );
    assert!(
        verify_instruction_sources(Some(&json!(["/repo/README.md"])))
            .unwrap()
            .contains("unexpected instruction source")
    );

    let declared = json!({"multi_agent": false, "hooks": false, "unified_exec": false});
    let reported = |over: &[(&str, bool)]| {
        let mut base = vec![
            ("multi_agent", false),
            ("hooks", false),
            ("unified_exec", true),
        ];
        for (k, v) in over {
            base.iter_mut().find(|(n, _)| n == k).unwrap().1 = *v;
        }
        Value::Array(
            base.into_iter()
                .map(|(name, enabled)| json!({"name": name, "enabled": enabled, "stage": "stable", "defaultEnabled": true}))
                .collect(),
        )
    };
    assert_eq!(
        verify_features(Some(&declared), Some(&reported(&[]))),
        None,
        "honoured, unified_exec exempt"
    );
    assert_eq!(
        verify_features(Some(&declared), Some(&reported(&[("multi_agent", true)]))).as_deref(),
        Some("features not honoured: multi_agent=true (declared false)")
    );
    assert!(verify_features(
        Some(&declared),
        Some(&reported(&[("multi_agent", true), ("hooks", true)]))
    )
    .unwrap()
    .contains("hooks=true (declared false)"));
    assert_eq!(
        verify_features(Some(&declared), Some(&json!([]))),
        None,
        "unreported flag is unknown"
    );
    assert_eq!(verify_features(Some(&declared), None), None);

    let h = |k: &'static str, v: &'static str| {
        let mut m = axum::http::HeaderMap::new();
        m.insert(k, v.parse().unwrap());
        m
    };
    assert_eq!(
        presented_key(&h("authorization", "Bearer sekret")).as_deref(),
        Some("sekret")
    );
    assert_eq!(
        presented_key(&h("x-api-key", "sekret")).as_deref(),
        Some("sekret")
    );
    assert_eq!(presented_key(&axum::http::HeaderMap::new()), None);

    assert_eq!(
        to_dynamic_tools(&[
            json!({"name": "Bash", "description": "run", "input_schema": {"type": "object"}})
        ]),
        vec![
            json!({"type": "function", "name": "Bash", "description": "run", "inputSchema": {"type": "object"}})
        ]
    );
    assert_eq!(
        count_tokens_estimate(
            &req(json!({"system": [{"type": "text", "text": "12345678"}], "messages": []})),
            &[]
        ),
        2
    );
}

#[test]
fn jsonl_log_is_0600_in_a_0700_dir_and_prunes_past_retention() {
    let dir = std::env::temp_dir().join(format!("codex-sidecar-log-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let stale = dir.join("codex-sidecar-2020-01-01.jsonl");
    std::fs::write(&stale, "{}\n").unwrap();

    let mut logger = Logger::new(&dir, 14.0);
    let mut obj = Map::new();
    obj.insert("port".into(), json!(0));
    logger.write("listen", &obj);
    logger.close();

    let today = dir.join(format!("codex-sidecar-{}.jsonl", Logger::day_of()));
    assert!(
        !stale.exists(),
        "files past SIDECAR_LOG_KEEP_DAYS are pruned"
    );
    assert_eq!(
        std::fs::metadata(&today).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
        0o700
    );
    let row: Value = serde_json::from_str(std::fs::read_to_string(&today).unwrap().trim()).unwrap();
    assert_eq!(row["kind"], "listen");
    let keys: Vec<&String> = row.as_object().unwrap().keys().collect();
    assert_eq!(
        keys,
        ["ts", "kind", "port"],
        "row key order is {{ts, kind, ...obj}}"
    );
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn usage_projects_into_anthropic_semantics() {
    let z = json!({"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0});
    assert_eq!(usage_out(None), z, "no usage yet");
    assert_eq!(
        usage_out(Some(&u(
            json!({"inputTokens": 6499, "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "outputTokens": 0})
        ))),
        json!({"input_tokens": 6499, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0})
    );
    let warm = usage_out(Some(&u(
        json!({"inputTokens": 6463, "cachedInputTokens": 3840, "cacheWriteInputTokens": 0, "outputTokens": 12}),
    )));
    assert_eq!(
        warm,
        json!({"input_tokens": 2623, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3840, "output_tokens": 12})
    );
    let sum = warm["input_tokens"].as_i64().unwrap()
        + warm["cache_read_input_tokens"].as_i64().unwrap()
        + warm["cache_creation_input_tokens"].as_i64().unwrap();
    assert_eq!(
        sum, 6463,
        "a client summing the three input columns gets the prompt exactly once"
    );
    assert_eq!(
        usage_out(Some(&u(
            json!({"inputTokens": 10000, "cachedInputTokens": 6000, "cacheWriteInputTokens": 1500, "outputTokens": 40})
        ))),
        json!({"input_tokens": 2500, "cache_creation_input_tokens": 1500, "cache_read_input_tokens": 6000, "output_tokens": 40})
    );
    assert_eq!(
        usage_out(Some(&u(
            json!({"inputTokens": 128, "cachedInputTokens": 3840, "cacheWriteInputTokens": 0, "outputTokens": 7})
        ))),
        json!({"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3840, "output_tokens": 7})
    );
    assert_eq!(
        usage_out(Some(&u(
            json!({"inputTokens": 0, "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "outputTokens": 5418})
        )))["output_tokens"],
        5418
    );
}

fn u(v: Value) -> Usage {
    Usage::from_value(&v)
}

#[test]
fn codex_usage_reads_missing_null_and_non_numeric_fields_as_zero() {
    assert_eq!(
        u(json!({"inputTokens": "12", "cachedInputTokens": null, "outputTokens": -3})),
        Usage::default()
    );
    assert_eq!(
        u(json!({"inputTokens": 7, "outputTokens": 2.0})),
        Usage {
            input: 7,
            output: 2,
            ..Usage::default()
        }
    );
}

#[test]
fn codex_usage_keeps_the_raw_object_beside_the_typed_counts() {
    let raw = json!({"inputTokens": 9, "cachedInputTokens": 4, "outputTokens": 3,
                     "reasoningOutputTokens": 1, "totalTokens": 12});
    let tu: TokenUsage = serde_json::from_value(json!({"total": raw, "last": 5})).unwrap();
    let total = tu.total.unwrap();
    assert_eq!(total.raw, raw, "every field codex sent, and only those");
    assert_eq!(
        total.usage,
        Usage {
            input: 9,
            cached: 4,
            cache_write: 0,
            output: 3
        }
    );
    assert!(tu.last.is_none(), "a non-object usage reads as absent");
}

#[test]
fn non_string_codex_ids_normalize_at_the_frame_boundary() {
    assert_eq!(
        ThreadId::from_value(&json!(42)),
        Some(ThreadId("42".into()))
    );
    assert_eq!(
        ThreadId::from_value(&json!("t-1")),
        Some(ThreadId("t-1".into()))
    );
    for v in [json!(""), json!(null), json!({"id": 1}), json!(true)] {
        assert_eq!(ThreadId::from_value(&v), None, "{v} reads as absent");
    }
    let call = ToolCall::of(
        &serde_json::from_value(json!({"id": 3, "method": "item/tool/call",
            "params": {"threadId": 7, "callId": 9, "turnId": {"x": 1}, "tool": "t"}}))
        .unwrap(),
    );
    assert_eq!(call.call_id(), CallId("9".into()));
    assert_eq!(call.params.thread_id, Some(ThreadId("7".into())));
    assert_eq!(
        call.params.turn_id, None,
        "a wrong-shape field never fails the frame"
    );
}
