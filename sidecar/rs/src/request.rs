//! request.mjs — phase 1 of the request path: what IS this request, and what should happen to
//! the session it names?
//!
//!   normalize_request()  the body read once into the view every later phase uses
//!   request_token()      that view's IDENTITY — the digest that decides join vs supersede
//!   disposition_of()     the ONE decision site: replay | join | drive(+supersedes)

use crate::errors::SidecarError;
use crate::js;
use crate::protocol::{self, Effort, ParsedModel, Scope, SessionKey, MODEL_FAMILIES};
use crate::session::id_key;
use crate::session::Session;
use axum::http::HeaderMap;
use serde_json::{json, Map, Value};

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

/// The request-shape check that runs before any session or sink is touched: `messages`, when
/// present, is an array of objects, and every element of an array `content` is an object.
pub fn validate(body: &Value) -> Result<(), SidecarError> {
    let bad = |m: String| SidecarError::new(400, "invalid_request_error", m);
    let msgs = match js::prop(Some(body), "messages") {
        None | Some(Value::Null) => return Ok(()),
        Some(Value::Array(a)) => a,
        Some(_) => return Err(bad("messages must be an array".into())),
    };
    for (i, m) in msgs.iter().enumerate() {
        let Value::Object(o) = m else {
            return Err(bad(format!("messages[{i}] must be an object")));
        };
        if let Some(Value::Array(blocks)) = o.get("content") {
            if let Some(j) = blocks.iter().position(|b| !b.is_object()) {
                return Err(bad(format!("messages[{i}].content[{j}] must be an object")));
            }
        }
    }
    Ok(())
}

/// How a request drives its session (logged as `as_str()`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    NewThread,
    NewThreadAfterRewind,
    ToolResult,
    NextTurn,
}

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NewThread => "new-thread",
            Self::NewThreadAfterRewind => "new-thread-after-rewind",
            Self::ToolResult => "tool-result",
            Self::NextTurn => "next-turn",
        }
    }

    pub fn is_new_thread(self) -> bool {
        matches!(self, Self::NewThread | Self::NewThreadAfterRewind)
    }
}

impl From<Mode> for Value {
    fn from(v: Mode) -> Self {
        Value::from(v.as_str())
    }
}

/// The body, read once (`normalizeRequest`).
#[derive(Debug, Clone)]
pub struct Normalized {
    pub body: Value,
    pub headers: HeaderMap,
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

pub fn normalize_request(body: Value, headers: Option<HeaderMap>) -> Normalized {
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
    pub scope: Scope,
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
    pub req_id: String,
    pub mode: Mode,
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

/// One block's `{type, keys, len}` summary.
fn block_shape(b: &Value) -> Value {
    let mut m = Map::new();
    let keys: String = match b {
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
    Value::Object(m)
}

/// `requestToken({reqId, mode, norm, ident})`; the body passed [`validate`].
pub fn request_token(req_id: String, mode: Mode, norm: &Normalized, ident: &Identity) -> Token {
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
        let blocks: Vec<Value> = blocks.iter().map(block_shape).collect();
        let mut row = Map::new();
        row.insert("d".into(), Value::from(d));
        if let Some(r) = js::prop(Some(m), "role") {
            row.insert("role".into(), r.clone());
        }
        row.insert("blocks".into(), Value::Array(blocks));
        shape.push(Value::Object(row));
    }
    Token {
        req_id,
        mode,
        n_messages: norm.msgs.len(),
        tool_result_ids: ids,
        digest,
        parts,
        msg_shape: Value::Array(shape),
    }
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

pub enum Disposition<'a> {
    Replay {
        outcome: &'a crate::session::LastOutcome,
    },
    Join {
        owner: &'a crate::session::OwnerTok,
    },
    Drive {
        supersedes: Option<&'a crate::session::OwnerTok>,
    },
}

/// THE decision: replay (straggler retry of the completed turn), join (byte-equal undecorated
/// twin of the driving request), or drive (superseding any owner).
pub fn disposition_of<'a>(sess: &'a Session, token: &Token) -> Disposition<'a> {
    if sess.owner.is_none() {
        if let Some(lo) = &sess.last_outcome {
            let straggler = !sess.turn_open
                && sess.parked.is_empty()
                && !token.tool_result_ids.is_empty()
                && token.digest == lo.digest
                && token
                    .tool_result_ids
                    .iter()
                    .all(|id| sess.delivered.contains(&id_key(id)));
            if straggler {
                return Disposition::Replay { outcome: lo };
            }
        }
    }
    if let Some(o) = &sess.owner {
        if o.token.mode == token.mode && o.token.digest == token.digest {
            return Disposition::Join { owner: o };
        }
    }
    Disposition::Drive {
        supersedes: sess.owner.as_ref(),
    }
}

// ------------------------------------------------------------- turn input ----

/// `rendersTranscript(mode, msgs)`: a new thread opening mid-conversation.
pub fn renders_transcript(mode: Mode, msgs: &[Value]) -> bool {
    mode.is_new_thread()
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

/// Upper bound on [`render_transcript`] output, in UTF-16 code units (the JS string length,
/// conservative against codex's 1 MiB character limit), wrapper and elision marker included.
pub const TRANSCRIPT_MAX_CHARS: usize = 240_000;

/// The elision line that opens `<prior-conversation>` when earlier messages were dropped.
pub fn elision_marker(omitted: usize) -> String {
    format!("[{omitted} earlier message(s) omitted: transcript capped at {TRANSCRIPT_MAX_CHARS} characters]")
}

/// `renderTranscript(msgs)`: the conversation, role-labeled, bounded by
/// [`TRANSCRIPT_MAX_CHARS`] UTF-16 code units. Under budget the output is the whole
/// transcript, unchanged. Over budget the wrapper, the elision marker and the final message
/// are reserved first, then prior messages are admitted newest-first into what remains (so
/// they drop oldest-first and keep their order), and the marker states how many were omitted.
/// The final message is never cut: if it alone exceeds the budget, every prior message is
/// elided and the overflow error mapping covers codex's rejection.
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
    let wrap = |body: &str| {
        format!(
            "<prior-conversation>\nThis thread was (re)started mid-conversation. The transcript so far:\n\n{body}\n</prior-conversation>\n\n{fin}"
        )
    };
    let full = wrap(&prior.join("\n\n"));
    if js::utf16_len(&full) <= TRANSCRIPT_MAX_CHARS {
        return full;
    }
    // framing + final + the widest marker this request could need
    let reserved = js::utf16_len(&wrap("")) + js::utf16_len(&elision_marker(prior.len()));
    let mut room = TRANSCRIPT_MAX_CHARS.saturating_sub(reserved);
    let mut keep = 0;
    for p in prior.iter().rev() {
        let cost = js::utf16_len(p) + 2; // "\n\n" separator
        if cost > room {
            break;
        }
        room -= cost;
        keep += 1;
    }
    let mut body = vec![elision_marker(prior.len() - keep)];
    body.extend(prior[prior.len() - keep..].iter().cloned());
    wrap(&body.join("\n\n"))
}

/// `turnInputText(mode, msgs, last, lastBlocks)`: the text one turn carries.
pub fn turn_input_text(
    mode: Mode,
    msgs: &[Value],
    last: Option<&Value>,
    last_blocks: &[Value],
) -> String {
    let text = if renders_transcript(mode, msgs) {
        render_transcript(msgs)
    } else if mode.is_new_thread() {
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
