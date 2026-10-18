//! Local leg: the 0007/0009/0010 cases, the system-merge edge cases, and
//! a paused-time keepalive test. Pure delegate tests; no network.

use bytes::Bytes;
use futures_util::StreamExt;
use inference_router::config::{Credential, EffortKwargs, Kind, MaxOutputTokens, Provider};
use inference_router::local_leg::{
    apply_effort_kwargs, apply_max_tokens_clamp, derive_effort_level, keepalive,
    merge_system_messages, transform_body, wrap_stream, LocalLegContext, KEEPALIVE_INTERVAL,
    PING_FRAME,
};
use inference_router::relay::{ByteStream, Route};
use serde_json::{json, Value};
use std::io;
use std::sync::Arc;
use std::time::Duration;

const M: &str = "qwen38-27b-iq3s";

fn typed(v: Value) -> EffortKwargs {
    serde_json::from_value(v).unwrap()
}

fn four_rows() -> EffortKwargs {
    typed(json!({ M: {
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
        "high": {"reasoning_effort": "high"},
        "disabled": {"enable_thinking": false}
    }}))
}

fn six_rows() -> EffortKwargs {
    typed(json!({ M: {
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "low"},
        "high": {"reasoning_effort": "medium"},
        "xhigh": {"reasoning_effort": "xhigh"},
        "max": {"reasoning_effort": "xhigh"},
        "disabled": {"enable_thinking": false}
    }}))
}

fn effort(map: &EffortKwargs, body: Value) -> Value {
    let mut b = body;
    apply_effort_kwargs(&mut b, map, M);
    b
}

fn clamp(body: Value, cap: Value) -> Value {
    let mut b = body;
    apply_max_tokens_clamp(
        &mut b,
        &MaxOutputTokens::from([(M.into(), cap.as_u64().unwrap())]),
        M,
    );
    b
}

fn ctx(route: Route, streaming: bool) -> LocalLegContext {
    LocalLegContext {
        provider: Arc::new(Provider {
            name: "claude-local".into(),
            kind: Kind::OpenAiChat,
            credential: Credential::ProviderKey,
            apikey: None,
            baseurl: None,
            models: vec![M.into()],
            native_messages: true,
            effort_kwargs: six_rows(),
            max_output_tokens: MaxOutputTokens::from([(M.into(), 32000)]),
        }),
        model: M.into(),
        route,
        streaming,
    }
}

fn eff(e: &str) -> Value {
    json!({"output_config": {"effort": e}})
}

// ---- 0007 / 0009 derivation ---------------------------------------------------------------

#[test]
fn maps_output_config_effort_low_medium_high_onto_themselves() {
    for l in ["low", "medium", "high"] {
        assert_eq!(derive_effort_level(&eff(l)), Some(l));
    }
}

#[test]
fn derives_max_and_xhigh_as_their_own_levels_case_and_whitespace_insensitively() {
    assert_eq!(derive_effort_level(&eff(" MAX ")), Some("max"));
    assert_eq!(derive_effort_level(&eff("XHigh")), Some("xhigh"));
}

#[test]
fn derives_disabled_from_thinking_type_ahead_of_any_effort() {
    let b = json!({"thinking": {"type": "disabled"}, "output_config": {"effort": "high"}});
    assert_eq!(derive_effort_level(&b), Some("disabled"));
}

#[test]
fn derives_no_level_from_adaptive_absent_or_unknown_intent() {
    assert_eq!(
        derive_effort_level(&json!({"thinking": {"type": "adaptive"}})),
        None
    );
    assert_eq!(derive_effort_level(&json!({})), None);
    assert_eq!(derive_effort_level(&eff("none")), None);
}

#[test]
fn maps_thinking_budget_tokens_at_the_documented_cut_points() {
    let b = |n: i64| json!({"thinking": {"type": "enabled", "budget_tokens": n}});
    assert_eq!(derive_effort_level(&b(1024)), Some("low"));
    assert_eq!(derive_effort_level(&b(2047)), Some("low"));
    assert_eq!(derive_effort_level(&b(2048)), Some("medium"));
    assert_eq!(derive_effort_level(&b(8191)), Some("medium"));
    assert_eq!(derive_effort_level(&b(8192)), Some("high"));
    assert_eq!(derive_effort_level(&b(64000)), Some("high"));
}

#[test]
fn ignores_a_non_numeric_or_absent_budget_on_thinking_enabled() {
    let b = json!({"thinking": {"type": "enabled", "budget_tokens": "1024"}});
    assert_eq!(derive_effort_level(&b), None);
    assert_eq!(
        derive_effort_level(&json!({"thinking": {"type": "enabled"}})),
        None
    );
}

#[test]
fn prefers_an_explicit_effort_over_the_budget_table() {
    let b = json!({"thinking": {"type": "enabled", "budget_tokens": 1024}, "output_config": {"effort": "high"}});
    assert_eq!(derive_effort_level(&b), Some("high"));
}

// ---- 0007 / 0009 application ----------------------------------------------------------------

#[test]
fn merges_the_mapped_kwargs_for_each_level() {
    let m = four_rows();
    assert_eq!(
        effort(&m, eff("low"))["chat_template_kwargs"],
        json!({"reasoning_effort": "low"})
    );
    assert_eq!(
        effort(&m, eff("high"))["chat_template_kwargs"],
        json!({"reasoning_effort": "high"})
    );
    let d = effort(&m, json!({"thinking": {"type": "disabled"}}));
    assert_eq!(d["chat_template_kwargs"], json!({"enable_thinking": false}));
}

#[test]
fn leaves_the_rest_of_the_body_including_the_model_id_untouched() {
    let b = effort(
        &four_rows(),
        json!({"model": M, "max_tokens": 9, "output_config": {"effort": "low"}}),
    );
    assert_eq!(b["model"], M);
    assert_eq!(b["max_tokens"], 9);
    assert_eq!(b["output_config"], json!({"effort": "low"}));
}

#[test]
fn is_a_no_op_without_a_map_for_an_unmapped_model_and_for_an_omitted_level() {
    let b = eff("low");
    assert_eq!(effort(&EffortKwargs::new(), b.clone()), b);
    let mut c = b.clone();
    apply_effort_kwargs(&mut c, &four_rows(), "other-model");
    assert_eq!(c, b);
    let partial = typed(json!({ M: {"high": {"reasoning_effort": "high"}}}));
    assert_eq!(effort(&partial, b.clone()), b);
}

#[test]
fn lets_keys_already_on_the_body_win_deeply() {
    let m = typed(json!({ M: {"low": {"reasoning_effort": "low", "nested": {"a": 1, "b": 2}}}}));
    let b = json!({"output_config": {"effort": "low"}, "chat_template_kwargs": {"reasoning_effort": "medium", "nested": {"b": 9}}});
    assert_eq!(
        effort(&m, b)["chat_template_kwargs"],
        json!({"reasoning_effort": "medium", "nested": {"a": 1, "b": 9}})
    );
}

#[test]
fn uses_the_exact_xhigh_and_max_rows_when_the_model_declares_them() {
    let m = typed(
        json!({ M: {"high": {"reasoning_effort": "high"}, "xhigh": {"reasoning_effort": "xhigh"}, "max": {"reasoning_effort": "max"}}}),
    );
    assert_eq!(
        effort(&m, eff("xhigh"))["chat_template_kwargs"]["reasoning_effort"],
        "xhigh"
    );
    assert_eq!(
        effort(&m, eff("max"))["chat_template_kwargs"]["reasoning_effort"],
        "max"
    );
}

#[test]
fn falls_back_to_the_high_row_for_xhigh_max_on_a_map_that_declares_neither() {
    let m = four_rows();
    assert_eq!(
        effort(&m, eff("xhigh"))["chat_template_kwargs"]["reasoning_effort"],
        "high"
    );
    assert_eq!(
        effort(&m, eff("max"))["chat_template_kwargs"]["reasoning_effort"],
        "high"
    );
}

#[test]
fn never_falls_back_for_a_level_that_is_not_xhigh_max() {
    let m = typed(json!({ M: {"high": {"reasoning_effort": "high"}}}));
    for l in ["low", "medium"] {
        assert!(effort(&m, eff(l)).get("chat_template_kwargs").is_none());
    }
    assert!(effort(&m, json!({"thinking": {"type": "disabled"}}))
        .get("chat_template_kwargs")
        .is_none());
}

#[test]
fn is_still_a_passthrough_for_xhigh_max_when_the_map_declares_no_high_row_either() {
    let m = typed(json!({ M: {"low": {"reasoning_effort": "low"}}}));
    assert_eq!(effort(&m, eff("max")), eff("max"));
}

#[test]
fn carries_the_six_row_total_map_through() {
    let m = six_rows();
    let want = [
        ("low", "low"),
        ("medium", "low"),
        ("high", "medium"),
        ("xhigh", "xhigh"),
        ("max", "xhigh"),
    ];
    for (l, kw) in want {
        assert_eq!(
            effort(&m, eff(l))["chat_template_kwargs"]["reasoning_effort"],
            kw,
            "{l}"
        );
    }
}

// ---- 0010 clamp ------------------------------------------------------------------------------

#[test]
fn clamps_a_request_above_the_declared_cap() {
    assert_eq!(
        clamp(json!({"max_tokens": 32000}), json!(8192))["max_tokens"],
        8192
    );
}

#[test]
fn passes_a_budget_at_or_below_the_cap_through_and_never_raises_it() {
    assert_eq!(
        clamp(json!({"max_tokens": 256}), json!(8192)),
        json!({"max_tokens": 256})
    );
    assert_eq!(
        clamp(json!({"max_tokens": 8192}), json!(8192)),
        json!({"max_tokens": 8192})
    );
}

#[test]
fn sends_the_declared_cap_when_the_request_carries_no_budget() {
    assert_eq!(clamp(json!({}), json!(8192))["max_tokens"], 8192);
    assert_eq!(
        clamp(json!({"max_tokens": null}), json!(8192))["max_tokens"],
        8192
    );
}

#[test]
fn leaves_a_present_but_unreadable_budget_alone() {
    assert_eq!(
        clamp(json!({"max_tokens": "32000"}), json!(8192))["max_tokens"],
        "32000"
    );
    assert_eq!(
        clamp(json!({"max_tokens": -5}), json!(8192))["max_tokens"],
        -5
    );
}

#[test]
fn clamp_is_idempotent_and_leaves_other_keys_untouched() {
    let once = clamp(
        json!({"max_tokens": 64000, "stream": true, "model": M}),
        json!(8192),
    );
    assert_eq!(clamp(once.clone(), json!(8192)), once);
    assert_eq!(
        once,
        json!({"max_tokens": 8192, "stream": true, "model": M})
    );
}

#[test]
fn clamp_is_a_no_op_without_a_map_or_for_an_omitted_model() {
    let b = json!({"max_tokens": 64000});
    let mut c = b.clone();
    apply_max_tokens_clamp(&mut c, &MaxOutputTokens::new(), M);
    assert_eq!(c, b);
    apply_max_tokens_clamp(&mut c, &MaxOutputTokens::from([("other".into(), 10)]), M);
    assert_eq!(c, b);
}

// ---- system-message merge --------------------------------------------------------------------

fn merged(body: Value) -> Value {
    let mut b = body;
    merge_system_messages(&mut b);
    b
}

#[test]
fn trailing_system_folds_into_the_preceding_user_turn_in_order() {
    let b = merged(
        json!({"system": "top", "messages": [{"role": "user", "content": "question"}, {"role": "system", "content": "late reminder"}]}),
    );
    assert_eq!(b["system"], "top");
    assert_eq!(
        b["messages"],
        json!([{"role": "user", "content": [{"type": "text", "text": "question"}, {"type": "text", "text": "late reminder"}]}])
    );
}

#[test]
fn consecutive_system_messages_all_land_in_order_with_block_content() {
    let b = merged(json!({"messages": [
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {"role": "system", "content": [{"type": "text", "text": "s1"}]},
        {"role": "system", "content": "s2"}
    ]}));
    let texts: Vec<&str> = b["messages"][0]["content"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x["text"].as_str().unwrap())
        .collect();
    assert_eq!(texts, ["q", "s1", "s2"]);
    assert_eq!(b["messages"].as_array().unwrap().len(), 1);
}

#[test]
fn leading_system_messages_fold_into_top_level_system() {
    let b = merged(
        json!({"system": "top", "messages": [{"role": "system", "content": "a"}, {"role": "system", "content": "b"}, {"role": "user", "content": "q"}]}),
    );
    assert_eq!(b["system"], "top\n\na\n\nb");
    assert_eq!(b["messages"], json!([{"role": "user", "content": "q"}]));
    let c = merged(
        json!({"messages": [{"role": "system", "content": "only"}, {"role": "user", "content": "q"}]}),
    );
    assert_eq!(c["system"], "only");
    let d = merged(
        json!({"system": [{"type": "text", "text": "t"}], "messages": [{"role": "system", "content": "x"}, {"role": "user", "content": "q"}]}),
    );
    assert_eq!(
        d["system"],
        json!([{"type": "text", "text": "t"}, {"type": "text", "text": "x"}])
    );
}

#[test]
fn system_after_an_assistant_turn_prepends_to_the_next_user_turn() {
    let b = merged(json!({"messages": [
        {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
        {"role": "system", "content": "s"}, {"role": "user", "content": "q2"}
    ]}));
    assert_eq!(
        b["messages"][2],
        json!({"role": "user", "content": [{"type": "text", "text": "s"}, {"type": "text", "text": "q2"}]})
    );
    assert_eq!(b["messages"].as_array().unwrap().len(), 3);
}

#[test]
fn trailing_system_after_an_assistant_turn_becomes_a_user_turn() {
    let b = merged(
        json!({"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}, {"role": "system", "content": "s"}]}),
    );
    assert_eq!(
        b["messages"][2],
        json!({"role": "user", "content": [{"type": "text", "text": "s"}]})
    );
}

#[test]
fn a_body_without_inner_system_messages_is_untouched() {
    let b = json!({"system": "x", "messages": [{"role": "user", "content": "q"}]});
    assert_eq!(merged(b.clone()), b);
}

// ---- body transform --------------------------------------------------------------------------

#[test]
fn transform_applies_merge_effort_and_clamp_on_messages() {
    let b = json!({"model": M, "max_tokens": 64000, "output_config": {"effort": "max"}, "messages": [{"role": "user", "content": "q"}, {"role": "system", "content": "s"}]});
    let out = transform_body(b, &ctx(Route::Messages, false));
    assert_eq!(out["max_tokens"], 32000);
    assert_eq!(
        out["chat_template_kwargs"],
        json!({"reasoning_effort": "xhigh"})
    );
    assert_eq!(out["messages"].as_array().unwrap().len(), 1);
}

#[test]
fn count_tokens_gets_merge_and_effort_but_not_the_clamp() {
    let b = json!({"model": M, "thinking": {"type": "disabled"}, "messages": [{"role": "user", "content": "q"}, {"role": "system", "content": "s"}]});
    let out = transform_body(b, &ctx(Route::CountTokens, false));
    assert!(out.get("max_tokens").is_none());
    assert_eq!(
        out["chat_template_kwargs"],
        json!({"enable_thinking": false})
    );
    assert_eq!(out["messages"].as_array().unwrap().len(), 1);
}

// ---- keepalive -------------------------------------------------------------------------------

// Real time with a scaled-down interval (tokio `test-util` is a dev-dependency, but this test
// keeps the real clock). 15 s itself is pinned by the harness case.
#[tokio::test]
async fn keepalive_pings_after_silence_repeating_and_passes_bytes_through() {
    let tick = Duration::from_millis(100);
    let a = futures_util::stream::iter(vec![Ok::<_, io::Error>(Bytes::from_static(b"a"))]);
    let b = futures_util::stream::once(async move {
        tokio::time::sleep(tick * 2 + tick / 2).await;
        Ok(Bytes::from_static(b"b"))
    });
    let upstream: ByteStream = Box::pin(a.chain(b));
    let s = keepalive(upstream, tick);
    let got: Vec<Bytes> = s.map(|r| r.unwrap()).collect().await;
    let ping = Bytes::from_static(PING_FRAME);
    let want = vec![
        Bytes::from_static(b"a"),
        ping.clone(),
        ping,
        Bytes::from_static(b"b"),
    ];
    assert_eq!(got, want);
    assert_eq!(PING_FRAME, b"event: ping\ndata: {\"type\":\"ping\"}\n\n");
    assert_eq!(KEEPALIVE_INTERVAL, Duration::from_secs(15));
    // Non-streaming and count_tokens are never wrapped.
    let quiet: ByteStream = Box::pin(futures_util::stream::once(async move {
        tokio::time::sleep(tick * 2).await;
        Ok(Bytes::from_static(b"z"))
    }));
    let n = wrap_stream(quiet, &ctx(Route::Messages, false))
        .count()
        .await;
    assert_eq!(n, 1);
    // Fast frames never ping.
    let fast: ByteStream = Box::pin(futures_util::stream::iter(
        (0..3).map(|_| Ok(Bytes::from_static(b"x"))),
    ));
    let all: Vec<_> = keepalive(fast, Duration::from_secs(15)).collect().await;
    assert_eq!(all.len(), 3);
}
