//! request.mjs — phase 1 of the request path: what IS this request, and what should happen to
//! the session it names?
//!
//!   normalize_request()  the body read once into the view every later phase uses
//!   request_token()      that view's IDENTITY — the digest that decides join vs supersede
//!   disposition_of()     the ONE decision site: replay | join | drive(+supersedes)

use crate::errors::SidecarError;
use crate::js;
use crate::protocol::{self, Effort, ParsedModel, SessionKey, MODEL_FAMILIES};
use serde_json::{json, Map, Value};
use std::collections::HashSet;

/// `canon(v)`: object keys sorted (UTF-16 order, as `Array.prototype.sort`), arrays kept.
/// Re-serialized through `js::stringify`, so array-index keys still lead as in JS.
pub fn canon(v: &Value) -> Value {
    match v {
        Value::Array(a) => Value::Array(a.iter().map(canon).collect()),
        Value::Object(m) => {
            let mut ks: Vec<&String> = m.keys().collect();
            ks.sort_by(|a, b| js::cmp_utf16(a, b));
            Value::Object(ks.into_iter().map(|k| (k.clone(), canon(&m[k]))).collect())
        }
        other => other.clone(),
    }
}

/// `undecorateBlock`: drop `cache_control` from an object block.
fn undecorate_block(b: &Value) -> Value {
    match b {
        Value::Object(m) if m.contains_key("cache_control") => {
            let mut m = m.clone();
            m.shift_remove("cache_control");
            Value::Object(m)
        }
        other => other.clone(),
    }
}

/// `undecorateMessage`: drop `cache_control`, and `text` blocks after a `tool_result` in a
/// user message.
fn undecorate_message(m: &Value) -> Value {
    let Some(obj) = m.as_object() else {
        return m.clone();
    };
    let Some(Value::Array(content)) = obj.get("content") else {
        return m.clone();
    };
    let user = obj.get("role") == Some(&Value::from("user"));
    let mut after_result = false;
    let mut out = Vec::new();
    for b in content {
        let ty = js::prop(Some(b), "type");
        if user && after_result && ty == Some(&Value::from("text")) {
            continue;
        }
        if ty == Some(&Value::from("tool_result")) {
            after_result = true;
        }
        out.push(undecorate_block(b));
    }
    let mut obj = obj.clone();
    obj.insert("content".into(), Value::Array(out));
    Value::Object(obj)
}

/// The `requestDigest` input; `None` fields are JS `undefined` (omitted from the JSON).
#[derive(Debug, Default, Clone)]
pub struct DigestFields<'a> {
    pub model: Option<&'a Value>,
    pub effort: Option<Value>,
    pub system: Option<&'a str>,
    pub tools: Option<&'a Value>,
    pub messages: Option<&'a [Value]>,
}

/// `requestDigest(fields)`: SHA-256 hex of the canonical, undecorated fields.
pub fn request_digest(f: &DigestFields<'_>) -> String {
    let mut m = Map::new();
    if let Some(v) = f.model {
        m.insert("model".into(), v.clone());
    }
    if let Some(v) = &f.effort {
        m.insert("effort".into(), v.clone());
    }
    if let Some(v) = f.system {
        m.insert("system".into(), Value::from(v));
    }
    if let Some(v) = f.tools {
        let t = match v {
            Value::Array(a) => Value::Array(a.iter().map(undecorate_block).collect()),
            other => other.clone(),
        };
        m.insert("tools".into(), t);
    }
    if let Some(ms) = f.messages {
        m.insert(
            "messages".into(),
            Value::Array(ms.iter().map(undecorate_message).collect()),
        );
    }
    protocol::sha256_hex(&js::stringify(&canon(&Value::Object(m))))
}

/// `messageDigests(msgs)`: per-message digests of the undecorated conversation.
pub fn message_digests(msgs: &[Value]) -> Vec<String> {
    msgs.iter()
        .map(|m| protocol::sha256_hex(&js::stringify(&canon(&undecorate_message(m)))))
        .collect()
}

/// `shortDigest(d)`: the first 12 hex chars.
pub fn short_digest(d: &str) -> String {
    d.chars().take(12).collect()
}

/// The body, read once (`normalizeRequest`).
#[derive(Debug, Clone)]
pub struct Normalized {
    pub body: Value,
    pub headers: Map<String, Value>,
    pub system_text: String,
    pub all_msgs: Vec<Value>,
    pub msgs: Vec<Value>,
    pub last: Option<Value>,
    pub last_blocks: Vec<Value>,
    pub raw_cwd: Option<String>,
    pub interleaved_text: String,
    pub tool_results: Vec<Value>,
    pub stream: bool,
}

pub fn normalize_request(body: Value, headers: Option<Map<String, Value>>) -> Normalized {
    let system_text = protocol::system_text_of(&body);
    let all_msgs: Vec<Value> = js::prop(Some(&body), "messages")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let msgs = protocol::conversational(Some(&Value::Array(all_msgs.clone())));
    let last = msgs.last().cloned();
    let last_blocks = protocol::blocks_of(last.as_ref().and_then(|l| js::prop(Some(l), "content")));
    let tool_results = last_blocks
        .iter()
        .filter(|b| js::prop(Some(b), "type") == Some(&Value::from("tool_result")))
        .cloned()
        .collect();
    Normalized {
        raw_cwd: protocol::extract_cwd(&all_msgs, &system_text),
        interleaved_text: protocol::interleaved_system_text(&all_msgs),
        stream: js::truthy(js::prop(Some(&body), "stream")),
        headers: headers.unwrap_or_default(),
        body,
        system_text,
        all_msgs,
        msgs,
        last,
        last_blocks,
        tool_results,
    }
}

#[derive(Debug, Clone)]
pub struct Identity {
    pub parsed: ParsedModel,
    pub eff: Effort,
    pub key: String,
    pub weak: bool,
    pub scope: &'static str,
}

/// `resolveIdentity(norm)`: model, effort and session key; the 400 for an unknown model id.
pub fn resolve_identity(norm: &Normalized) -> Result<Identity, SidecarError> {
    let model = js::prop(Some(&norm.body), "model");
    let Some(parsed) = protocol::parse_model_id(model) else {
        let names: Vec<&str> = MODEL_FAMILIES.iter().map(|(n, _)| *n).collect();
        return Err(SidecarError::new(
            400,
            "invalid_request_error",
            format!(
                "unknown model id \"{}\" — expected one of {} with an optional -low/-medium/-high suffix",
                js::js_string_or_empty(model),
                names.join(", ")
            ),
        ));
    };
    let eff = protocol::resolve_effort(Some(&parsed), &norm.body);
    let first_user = norm
        .msgs
        .iter()
        .find(|m| js::prop(Some(m), "role") == Some(&Value::from("user")));
    let first_text = protocol::text_of(first_user.and_then(|m| js::prop(Some(m), "content")));
    let SessionKey { key, weak, scope } = protocol::session_key_of(
        &norm.body,
        &norm.system_text,
        &first_text,
        Some(&norm.headers),
    );
    Ok(Identity {
        parsed,
        eff,
        key,
        weak,
        scope,
    })
}

/// One request's identity and diagnostics (`requestToken`).
#[derive(Debug, Clone)]
pub struct Token {
    pub req_id: Value,
    pub mode: String,
    pub n_messages: usize,
    /// Truthy `tool_use_id`s, sorted as JS `sort()` does (by `String(v)`, UTF-16 order).
    pub tool_result_ids: Vec<Value>,
    pub digest: String,
    /// `{model, effort, system, tools, messages}` short digests (logged on supersede).
    pub parts: Value,
    /// Per-message structural summary (never content), as the log row writes it.
    pub msg_shape: Value,
}

fn effort_value(e: &Effort) -> Value {
    e.effort.as_deref().map_or(Value::Null, Value::from)
}

fn type_error_null(prop: &str) -> SidecarError {
    crate::errors::as_sidecar_error(&format!(
        "Cannot read properties of null (reading '{prop}')"
    ))
}

/// One block's `{type, keys, len}` summary.
fn block_shape(b: &Value) -> Result<Value, SidecarError> {
    let mut m = Map::new();
    let keys: String = match b {
        Value::Null => return Err(type_error_null("type")),
        Value::Object(o) => {
            if let Some(t) = o.get("type") {
                m.insert("type".into(), t.clone());
            }
            let mut ks: Vec<&String> = js::keys(o);
            ks.sort_by(|a, b| js::cmp_utf16(a, b));
            ks.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(",")
        }
        Value::Array(a) => {
            let mut ks: Vec<String> = (0..a.len()).map(|i| i.to_string()).collect();
            ks.sort_by(|a, b| js::cmp_utf16(a, b));
            ks.join(",")
        }
        Value::String(s) => {
            let mut ks: Vec<String> = (0..js::utf16_len(s)).map(|i| i.to_string()).collect();
            ks.sort_by(|a, b| js::cmp_utf16(a, b));
            ks.join(",")
        }
        _ => String::new(),
    };
    m.insert("keys".into(), Value::from(keys));
    let len = match (js::prop(Some(b), "text"), js::prop(Some(b), "content")) {
        (Some(Value::String(t)), _) => Value::from(js::utf16_len(t)),
        (_, Some(Value::String(c))) => Value::from(js::utf16_len(c)),
        (_, Some(Value::Array(c))) => Value::from(c.len()),
        _ => Value::Null,
    };
    m.insert("len".into(), len);
    Ok(Value::Object(m))
}

/// `requestToken({reqId, mode, norm, ident})`. Errs only where Node would throw a TypeError
/// (a `null` content block in `msgShape`), mapped as `asSidecarError` maps it.
pub fn request_token(
    req_id: Value,
    mode: &str,
    norm: &Normalized,
    ident: &Identity,
) -> Result<Token, SidecarError> {
    let model = js::prop(Some(&norm.body), "model");
    let empty = Value::Array(Vec::new());
    let tools = js::present(js::prop(Some(&norm.body), "tools")).unwrap_or(&empty);
    let effort = effort_value(&ident.eff);
    let mut ids: Vec<Value> = norm
        .tool_results
        .iter()
        .filter_map(|tr| js::prop(Some(tr), "tool_use_id"))
        .filter(|v| js::truthy(Some(v)))
        .cloned()
        .collect();
    ids.sort_by(|a, b| js::cmp_utf16(&js::js_string(a), &js::js_string(b)));
    let digest = request_digest(&DigestFields {
        model,
        effort: Some(effort.clone()),
        system: Some(&norm.system_text),
        tools: Some(tools),
        messages: Some(&norm.msgs),
    });
    let part = |f: DigestFields<'_>| Value::from(short_digest(&request_digest(&f)));
    let parts = json!({
        "model": part(DigestFields { model, ..Default::default() }),
        "effort": part(DigestFields { effort: Some(effort.clone()), ..Default::default() }),
        "system": part(DigestFields { system: Some(&norm.system_text), ..Default::default() }),
        "tools": part(DigestFields { tools: Some(tools), ..Default::default() }),
        "messages": part(DigestFields { messages: Some(&norm.msgs), ..Default::default() }),
    });
    let mut shape = Vec::new();
    for m in &norm.msgs {
        let d = short_digest(&protocol::sha256_hex(&js::stringify(&canon(m))));
        let content = js::prop(Some(m), "content");
        let blocks: Vec<Value> = match content {
            Some(Value::Array(a)) => a.clone(),
            other => vec![json!({ "type": "text", "text": js::js_string_or_empty(other) })],
        };
        let blocks = blocks
            .iter()
            .map(block_shape)
            .collect::<Result<Vec<_>, _>>()?;
        let mut row = Map::new();
        row.insert("d".into(), Value::from(d));
        if let Some(r) = js::prop(Some(m), "role") {
            row.insert("role".into(), r.clone());
        }
        row.insert("blocks".into(), Value::Array(blocks));
        shape.push(Value::Object(row));
    }
    Ok(Token {
        req_id,
        mode: mode.to_string(),
        n_messages: norm.msgs.len(),
        tool_result_ids: ids,
        digest,
        parts,
        msg_shape: Value::Array(shape),
    })
}

/// `msgDiff(a, b)`: up to four indices where two `msg_shape` summaries differ.
pub fn msg_diff(a: Option<&Value>, b: Option<&Value>) -> Option<Value> {
    let a = a.filter(|v| js::truthy(Some(v)))?.as_array()?;
    let b = b.filter(|v| js::truthy(Some(v)))?;
    let bv = b.as_array();
    let out: Vec<Value> = a
        .iter()
        .enumerate()
        .filter_map(|(i, x)| {
            let y = bv.and_then(|v| v.get(i));
            let differs = y.is_none_or(|y| js::prop(Some(x), "d") != js::prop(Some(y), "d"));
            differs.then(|| {
                let mut m = Map::new();
                m.insert("i".into(), Value::from(i));
                if let Some(r) = js::prop(Some(x), "role") {
                    m.insert("role".into(), r.clone());
                }
                if let Some(r) = js::prop(Some(x), "blocks") {
                    m.insert("req".into(), r.clone());
                }
                let owner = js::present(js::prop(y, "blocks"))
                    .cloned()
                    .unwrap_or(Value::Null);
                m.insert("owner".into(), owner);
                Value::Object(m)
            })
        })
        .take(4)
        .collect();
    Some(Value::Array(out))
}

// ------------------------------------------------------------- disposition ----

/// What `dispositionOf` reads from a session; WP-3's `Session` implements it.
pub trait SessionState {
    type Owner;
    type Outcome;
    fn owner(&self) -> Option<&Self::Owner>;
    fn owner_mode(owner: &Self::Owner) -> &str;
    fn owner_digest(owner: &Self::Owner) -> &str;
    fn last_outcome(&self) -> Option<&Self::Outcome>;
    fn outcome_digest(outcome: &Self::Outcome) -> &str;
    fn turn_open(&self) -> bool;
    fn parked_len(&self) -> usize;
    fn delivered(&self) -> &HashSet<String>;
}

pub enum Disposition<'a, S: SessionState> {
    Replay { outcome: &'a S::Outcome },
    Join { owner: &'a S::Owner },
    Drive { supersedes: Option<&'a S::Owner> },
}

fn id_key(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => js::js_string(other),
    }
}

/// THE decision: replay (straggler retry of the completed turn), join (byte-equal undecorated
/// twin of the driving request), or drive (superseding any owner).
pub fn disposition_of<'a, S: SessionState>(sess: &'a S, token: &Token) -> Disposition<'a, S> {
    if sess.owner().is_none() {
        if let Some(lo) = sess.last_outcome() {
            let straggler = !sess.turn_open()
                && sess.parked_len() == 0
                && !token.tool_result_ids.is_empty()
                && token.digest == S::outcome_digest(lo)
                && token
                    .tool_result_ids
                    .iter()
                    .all(|id| sess.delivered().contains(&id_key(id)));
            if straggler {
                return Disposition::Replay { outcome: lo };
            }
        }
    }
    if let Some(o) = sess.owner() {
        if S::owner_mode(o) == token.mode && S::owner_digest(o) == token.digest {
            return Disposition::Join { owner: o };
        }
    }
    Disposition::Drive {
        supersedes: sess.owner(),
    }
}

// ------------------------------------------------------------- turn input ----

/// `rendersTranscript(mode, msgs)`: a new thread opening mid-conversation.
pub fn renders_transcript(mode: &str, msgs: &[Value]) -> bool {
    mode.starts_with("new-thread")
        && msgs
            .iter()
            .any(|m| js::prop(Some(m), "role") == Some(&Value::from("assistant")))
}

fn or_q(v: Option<&Value>) -> String {
    js::present(v).map_or_else(|| "?".into(), js::js_string)
}

fn render_block(b: &Value) -> String {
    if !b.is_object() && !b.is_array() {
        return String::new();
    }
    let ty = js::prop(Some(b), "type").and_then(Value::as_str);
    match ty {
        Some("text") => js::prop(Some(b), "text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        Some("tool_use") => {
            let input = js::present(js::prop(Some(b), "input"))
                .cloned()
                .unwrap_or_else(|| json!({}));
            format!(
                "[tool_use {} id={}] {}",
                or_q(js::prop(Some(b), "name")),
                or_q(js::prop(Some(b), "id")),
                js::stringify(&input)
            )
        }
        Some("tool_result") => {
            let c = js::prop(Some(b), "content");
            let body = match c {
                Some(Value::String(s)) => s.clone(),
                other => protocol::text_of(other),
            };
            let err = if js::truthy(js::prop(Some(b), "is_error")) {
                " is_error"
            } else {
                ""
            };
            format!(
                "[tool_result id={}{err}]\n{body}",
                or_q(js::prop(Some(b), "tool_use_id"))
            )
        }
        _ => String::new(),
    }
}

fn render_blocks(content: Option<&Value>) -> String {
    protocol::blocks_of(content)
        .iter()
        .map(render_block)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

/// `renderTranscript(msgs)`: the whole conversation, role-labeled, never truncated.
pub fn render_transcript(msgs: &[Value]) -> String {
    let prior: Vec<String> = msgs
        .iter()
        .take(msgs.len().saturating_sub(1))
        .map(|m| {
            format!(
                "### {}\n{}",
                js::prop(Some(m), "role").map_or_else(|| "undefined".into(), js::js_string),
                render_blocks(js::prop(Some(m), "content"))
            )
        })
        .collect();
    let fin = render_blocks(msgs.last().and_then(|m| js::prop(Some(m), "content")));
    format!(
        "<prior-conversation>\nThis thread was (re)started mid-conversation. The transcript so far:\n\n{}\n</prior-conversation>\n\n{fin}",
        prior.join("\n\n")
    )
}

/// `turnInputText(mode, msgs, last, lastBlocks)`: the text one turn carries.
pub fn turn_input_text(
    mode: &str,
    msgs: &[Value],
    last: Option<&Value>,
    last_blocks: &[Value],
) -> String {
    let text = if renders_transcript(mode, msgs) {
        render_transcript(msgs)
    } else if mode.starts_with("new-thread") {
        msgs.iter()
            .filter(|m| js::prop(Some(m), "role") == Some(&Value::from("user")))
            .map(|m| protocol::text_of(js::prop(Some(m), "content")))
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("\n\n")
    } else {
        let t = protocol::text_of(last.and_then(|l| js::prop(Some(l), "content")));
        if js::trim(&t).is_empty() {
            last_blocks
                .iter()
                .filter(|b| js::prop(Some(b), "type") == Some(&Value::from("tool_result")))
                .map(|b| match js::prop(Some(b), "content") {
                    Some(Value::String(s)) => s.clone(),
                    other => protocol::text_of(other),
                })
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
                .join("\n\n")
        } else {
            t
        }
    };
    if js::trim(&text).is_empty() {
        "(empty user message)".into()
    } else {
        text
    }
}
