//! Digest parity with the Node oracle. Expected values were measured once by importing
//! `sidecar/request.mjs` / `protocol.mjs` under Node on exactly these inputs (2026-10-18):
//! non-ASCII + astral text, `cache_control` decoration, a trailing post-`tool_result` text
//! block, integer-like keys and a 2e21 float.

use codex_sidecar::protocol::{count_tokens_estimate, session_key_of};
use codex_sidecar::request::{
    elision_marker, message_digests, render_transcript, request_digest, DigestFields,
    TRANSCRIPT_MAX_CHARS,
};
use serde_json::{json, Value};

fn msgs() -> Vec<Value> {
    vec![
        json!({"role": "user", "content": [{"type": "text", "text": "héllo 😀", "cache_control": {"type": "ephemeral"}}]}),
        json!({"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
               "input": {"b": 1.5, "a": [1, 2e21], "10": "x", "2": null}}]}),
        json!({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}, {"type": "text", "text": "reminder"}]}),
    ]
}

#[test]
fn request_and_message_digests_match_node() {
    let m = msgs();
    let model = json!("gpt-5.6-sol");
    let tools = json!([]);
    let d = request_digest(&DigestFields {
        model: Some(&model),
        effort: Some(json!("medium")),
        system: Some("sys"),
        tools: Some(&tools),
        messages: Some(&m),
    });
    assert_eq!(
        d,
        "986539c4bfc897a694536ca0057fed083d154baa016f5050093abea3857963b2"
    );
    assert_eq!(
        message_digests(&m),
        [
            "eca83eb978e309d8ab59e59e9ee91f6db62c68702df59bfd2069d766a3e525a8",
            "d0f1987977d3b6ba5976faa5604324a30a7e78e90d683058c0f029bd12ab9ea3",
            "7dd545fe8dd33bc61c60f2269c762615659d0b65ddb7ab0068af0a33da8c2fe1",
        ]
    );
}

#[test]
fn subagent_session_key_and_token_estimate_match_node() {
    let billing = "x-anthropic-billing-header: cc_version=1; cc_is_subagent=true;";
    let body = json!({
        "system": [{"type": "text", "text": billing}, {"type": "text", "text": "S"}],
        "metadata": {"user_id": {"session_id": "sid"}},
        "messages": msgs(),
    });
    let k = session_key_of(&body, &format!("{billing}\n\nS"), "héllo 😀", None);
    assert_eq!(k.key, "sid:9868bc96decaf055");
    assert_eq!(k.scope.as_str(), "subagent");
    assert_eq!(count_tokens_estimate(&body), 31.0);
}

// ---------------------------------------------------- rehydration cap (WP-3) ----

fn text_msg(role: &str, t: &str) -> Value {
    json!({"role": role, "content": [{"type": "text", "text": t}]})
}

fn u16len(s: &str) -> usize {
    s.encode_utf16().count()
}

const HEAD: &str =
    "<prior-conversation>\nThis thread was (re)started mid-conversation. The transcript so far:\n\n";

#[test]
fn transcript_under_budget_is_byte_identical() {
    let small = vec![
        text_msg("user", "a"),
        text_msg("assistant", "b"),
        text_msg("user", "c"),
    ];
    assert_eq!(
        render_transcript(&small),
        format!("{HEAD}### user\na\n\n### assistant\nb\n</prior-conversation>\n\nc")
    );
    // exactly at budget still renders whole, no marker
    let base = u16len(&render_transcript(&[
        text_msg("user", ""),
        text_msg("user", "z"),
    ]));
    let pad = "p".repeat(TRANSCRIPT_MAX_CHARS - base);
    let at = render_transcript(&[text_msg("user", &pad), text_msg("user", "z")]);
    assert_eq!(u16len(&at), TRANSCRIPT_MAX_CHARS);
    assert!(!at.contains("omitted"));
}

#[test]
fn transcript_over_budget_drops_oldest_with_marker() {
    let big = "x".repeat(50_000);
    let mut ms: Vec<Value> = (0..10)
        .map(|i| {
            let role = if i % 2 == 0 { "user" } else { "assistant" };
            text_msg(role, &format!("m{i} {big}"))
        })
        .collect();
    ms.push(text_msg("user", "final question"));
    let out = render_transcript(&ms);
    assert!(u16len(&out) <= TRANSCRIPT_MAX_CHARS, "{}", u16len(&out));
    let kept: Vec<usize> = (0..10)
        .filter(|i| out.contains(&format!("m{i} ")))
        .collect();
    assert_eq!(kept, vec![6, 7, 8, 9], "newest kept, oldest dropped");
    let marker = elision_marker(6);
    assert_eq!(
        marker,
        "[6 earlier message(s) omitted: transcript capped at 240000 characters]"
    );
    assert!(out.starts_with(&format!("{HEAD}{marker}\n\n### user\nm6 ")));
    assert!(out.ends_with("\n</prior-conversation>\n\nfinal question"));
    assert!(out.find("m8 ").unwrap() < out.find("m9 ").unwrap());
}

#[test]
fn transcript_oversize_final_elides_every_prior() {
    let huge = "y".repeat(TRANSCRIPT_MAX_CHARS + 10);
    let ms = vec![
        text_msg("user", "old"),
        text_msg("assistant", "older"),
        text_msg("user", &huge),
    ];
    assert_eq!(
        render_transcript(&ms),
        format!(
            "{HEAD}{}\n</prior-conversation>\n\n{huge}",
            elision_marker(2)
        )
    );
}

#[test]
fn validate_rejects_malformed_message_shapes_with_a_400() {
    use codex_sidecar::request::validate;
    assert!(validate(&json!({})).is_ok());
    assert!(validate(&json!({"messages": [{"role": "user", "content": "x"}]})).is_ok());
    for (body, msg) in [
        (json!({"messages": {}}), "messages must be an array"),
        (json!({"messages": ["x"]}), "messages[0] must be an object"),
        (
            json!({"messages": [{"role": "user", "content": "a"}, {"role": "user", "content": [{"type": "text", "text": "b"}, null]}]}),
            "messages[1].content[1] must be an object",
        ),
    ] {
        let e = validate(&body).expect_err(msg);
        assert_eq!(
            (e.status, e.anthropic_type.as_str(), e.message.as_str()),
            (400, "invalid_request_error", msg)
        );
    }
}
