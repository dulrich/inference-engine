//! Digests, the session key, the token estimate and the transcript cap. The digest values
//! are in-process only (pinned here as a regression check, re-baselined when the request was
//! typed); what must hold is their equivalence classes: `cache_control` and post-`tool_result`
//! text are ignored, key order is canonical, numbers compare by value, and every field the
//! typed request does not name still counts.

use codex_sidecar::protocol::{count_tokens_estimate, session_key_of};
use codex_sidecar::request::{
    elision_marker, message_digests, render_transcript, request_digest, DigestFields, Message,
    Request, TRANSCRIPT_MAX_CHARS,
};
use serde_json::{json, Value};

fn typed(v: Vec<Value>) -> Vec<Message> {
    serde_json::from_value(Value::Array(v)).unwrap()
}

fn raw_msgs() -> Vec<Value> {
    vec![
        json!({"role": "user", "content": [{"type": "text", "text": "héllo 😀", "cache_control": {"type": "ephemeral"}}]}),
        json!({"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
               "input": {"b": 1.5, "a": [1, 2e21], "10": "x", "2": null}}]}),
        json!({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}, {"type": "text", "text": "reminder"}]}),
    ]
}

fn msgs() -> Vec<Message> {
    typed(raw_msgs())
}

fn digest_of(m: Value) -> String {
    message_digests(&typed(vec![m])).remove(0)
}

#[test]
fn request_and_message_digests_are_stable() {
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
        "093a2b6cb94f69b43703bffabe14a9e3d72de432e309ec06f31589d1057256a8"
    );
    assert_eq!(
        message_digests(&m),
        [
            "eca83eb978e309d8ab59e59e9ee91f6db62c68702df59bfd2069d766a3e525a8",
            "0f69dfc61e9d403f2aa0788b7a2d988e60148b1777f41a04393c34c30d0ce662",
            "7dd545fe8dd33bc61c60f2269c762615659d0b65ddb7ab0068af0a33da8c2fe1",
        ]
    );
}

#[test]
fn subagent_session_key_and_token_estimate() {
    let billing = "x-anthropic-billing-header: cc_version=1; cc_is_subagent=true;";
    let body = Request::parse(json!({
        "system": [{"type": "text", "text": billing}, {"type": "text", "text": "S"}],
        "metadata": {"user_id": {"session_id": "sid"}},
        "messages": raw_msgs(),
    }))
    .unwrap();
    let k = session_key_of(&body, &format!("{billing}\n\nS"), "héllo 😀", None);
    assert_eq!(k.key, "sid:9868bc96decaf055");
    assert_eq!(k.scope.as_str(), "subagent");
    assert_eq!(count_tokens_estimate(&body, &body.messages), 31);
}

// ---------------------------------------------------------- rehydration cap ----

fn text_msg(role: &str, t: &str) -> Message {
    typed(vec![
        json!({"role": role, "content": [{"type": "text", "text": t}]}),
    ])
    .remove(0)
}

fn chars(s: &str) -> usize {
    s.chars().count()
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
    let base = chars(&render_transcript(&[
        text_msg("user", ""),
        text_msg("user", "z"),
    ]));
    let pad = "p".repeat(TRANSCRIPT_MAX_CHARS - base);
    let at = render_transcript(&[text_msg("user", &pad), text_msg("user", "z")]);
    assert_eq!(chars(&at), TRANSCRIPT_MAX_CHARS);
    assert!(!at.contains("omitted"));
}

#[test]
fn transcript_over_budget_drops_oldest_with_marker() {
    let big = "x".repeat(50_000);
    let mut ms: Vec<Message> = (0..10)
        .map(|i| {
            let role = if i % 2 == 0 { "user" } else { "assistant" };
            text_msg(role, &format!("m{i} {big}"))
        })
        .collect();
    ms.push(text_msg("user", "final question"));
    let out = render_transcript(&ms);
    assert!(chars(&out) <= TRANSCRIPT_MAX_CHARS, "{}", chars(&out));
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
fn parse_rejects_malformed_message_shapes_with_a_400() {
    let validate = |v: &Value| Request::parse(v.clone());
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

#[test]
fn transcript_cap_counts_chars_not_utf16_units() {
    // 200,000 astral chars are 400,000 UTF-16 units: under the char cap, so nothing is elided
    let astral = "😀".repeat(200_000);
    let out = render_transcript(&[text_msg("user", &astral), text_msg("user", "z")]);
    assert!(!out.contains("omitted"));
    assert!(chars(&out) <= TRANSCRIPT_MAX_CHARS);
}

/// E2: numbers digest by value
/// (measured on bfb0a27's `message_digests`: `1`≡`1.0`, `0`≡`-0`≡`-0.0`, 2^53+1≡2^53,
/// `1e21`≡`10^21`; each class distinct from the others).
#[test]
fn digest_numbers_compare_by_value() {
    let m = |n: &str| -> Value {
        serde_json::from_str(&format!(
            r#"{{"role":"user","content":[{{"type":"tool_use","id":"t","name":"B","input":{{"n":{n}}}}}]}}"#
        ))
        .unwrap()
    };
    let classes: &[&[&str]] = &[
        &["1", "1.0"],
        &["0", "-0", "-0.0"],
        &["9007199254740993", "9007199254740992"],
        &["1e21", "1000000000000000000000"],
        &["1.5"],
    ];
    let reps: Vec<String> = classes.iter().map(|c| digest_of(m(c[0]))).collect();
    for (class, rep) in classes.iter().zip(&reps) {
        for n in *class {
            assert_eq!(&digest_of(m(n)), rep, "{n} joins {}", class[0]);
        }
    }
    for (i, a) in reps.iter().enumerate() {
        assert!(!reps[i + 1..].contains(a), "class {i} is distinct");
    }
}

/// E2: a field the typed request does not name still contributes to the digest; only the
/// decorations (`cache_control`, post-`tool_result` text) are ignored.
#[test]
fn digest_keeps_identity_bearing_unknown_fields() {
    let base = digest_of(json!({"role": "user", "content": [{"type": "text", "text": "a"}]}));
    for differing in [
        json!({"role": "user", "content": [{"type": "text", "text": "a", "citations": [1]}]}),
        json!({"role": "user", "content": [{"type": "text", "text": "a"}], "name": "x"}),
        json!({"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "document", "source": {"data": "x"}}]}),
        json!({"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "document", "source": {"data": "y"}}]}),
        json!({"role": "user", "content": [{"type": "text", "text": 5}]}),
    ] {
        assert_ne!(digest_of(differing.clone()), base, "{differing}");
    }
    let doc = |d: &str| {
        digest_of(json!({"role": "user", "content": [{"type": "document", "source": {"data": d}}]}))
    };
    assert_ne!(doc("x"), doc("y"), "an unknown block's payload counts");
    assert_eq!(
        digest_of(
            json!({"role": "user", "content": [{"text": "a", "type": "text", "cache_control": {"type": "ephemeral"}}]})
        ),
        base,
        "cache_control and key order are not identity"
    );
    let tr = json!({"type": "tool_result", "tool_use_id": "t", "content": "ok"});
    assert_eq!(
        digest_of(json!({"role": "user", "content": [tr, {"type": "text", "text": "reminder"}]})),
        digest_of(json!({"role": "user", "content": [tr]})),
        "text after a tool_result is not identity"
    );
}
