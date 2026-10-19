//! request.mjs — phase 1 of the request path: what IS this request, and what should happen to
//! the session it names?
//!
//!   Request::parse()     the body parsed once into typed form (malformed shapes are a 400)
//!   normalize_request()  the typed request read once into the view every later phase uses
//!   request_token()      that view's IDENTITY — the digest that decides join vs supersede
//!   disposition_of()     the ONE decision site: replay | join | drive(+supersedes)

use crate::errors::SidecarError;
use crate::protocol::{self, Effort, ParsedModel, Scope, SessionKey, MODEL_FAMILIES};
use crate::session::Session;
use axum::http::HeaderMap;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{json, Map, Value};

// ---------------------------------------------------------- the request ----

/// A present field read as-is, an explicit `null` kept as `Some(Null)`: the typed view then
/// serializes back to exactly the JSON it was read from (digest identity).
fn keep<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Value>, D::Error> {
    Value::deserialize(d).map(Some)
}

/// A field whose wrong-typed value reads as its default instead of failing the parse.
fn lenient<'de, D: Deserializer<'de>, T: DeserializeOwned + Default>(d: D) -> Result<T, D::Error> {
    Ok(serde_json::from_value(Value::deserialize(d)?).unwrap_or_default())
}

fn keep_content<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Content>, D::Error> {
    Value::deserialize(d).map(|v| Some(Content::from_value(v)))
}

/// `stream`: a boolean; any other JSON value is read as the JS harness would test it.
fn flag<'de, D: Deserializer<'de>>(d: D) -> Result<bool, D::Error> {
    Ok(match Value::deserialize(d)? {
        Value::Null => false,
        Value::Bool(b) => b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(_) | Value::Object(_) => true,
    })
}

fn messages_or_empty<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<Message>, D::Error> {
    Ok(Option::<Vec<Message>>::deserialize(d)?.unwrap_or_default())
}

/// The Anthropic Messages request body, as this sidecar reads it.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Request {
    /// Kept as sent (a non-string id still names itself in the 400 and the digest).
    #[serde(default, deserialize_with = "keep")]
    pub model: Option<Value>,
    #[serde(default, deserialize_with = "lenient")]
    pub system: Option<System>,
    #[serde(default, deserialize_with = "messages_or_empty")]
    pub messages: Vec<Message>,
    /// Kept as sent: tools digest whole; only an array lists any (see [`Request::tool_list`]).
    #[serde(default, deserialize_with = "keep")]
    pub tools: Option<Value>,
    #[serde(default, deserialize_with = "flag")]
    pub stream: bool,
    #[serde(default, deserialize_with = "lenient")]
    pub metadata: Metadata,
    #[serde(default, deserialize_with = "lenient")]
    pub output_config: OutputConfig,
    #[serde(default, deserialize_with = "lenient")]
    pub thinking: Thinking,
}

/// `system`: a string, or an array of blocks (strings or `{text}` objects).
#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
pub enum System {
    Text(String),
    Blocks(Vec<Value>),
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Metadata {
    /// A JSON-encoded string or an object carrying `session_id`.
    #[serde(default)]
    pub user_id: Option<Value>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct OutputConfig {
    #[serde(default, deserialize_with = "lenient")]
    pub effort: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Thinking {
    #[serde(default, rename = "type", deserialize_with = "lenient")]
    pub kind: Option<String>,
}

/// One `messages[]` entry. `extra` carries every key the sidecar does not read, so the
/// message digests whole.
#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct Message {
    #[serde(
        default,
        deserialize_with = "keep",
        skip_serializing_if = "Option::is_none"
    )]
    pub role: Option<Value>,
    #[serde(
        default,
        deserialize_with = "keep_content",
        skip_serializing_if = "Option::is_none"
    )]
    pub content: Option<Content>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl Message {
    pub fn role(&self) -> Option<&str> {
        self.role.as_ref().and_then(Value::as_str)
    }

    pub fn to_value(&self) -> Value {
        serde_json::to_value(self).unwrap_or(Value::Null)
    }

    /// `blocksOf(content)`: the array's blocks; a string content is one text block.
    pub fn blocks(&self) -> Vec<ContentBlock> {
        self.content
            .as_ref()
            .map(Content::blocks)
            .unwrap_or_default()
    }
}

/// A message's (or a `tool_result`'s) `content`.
#[derive(Debug, Clone, PartialEq)]
pub enum Content {
    Text(String),
    Blocks(Vec<ContentBlock>),
    /// Any other JSON value (`null`, a number, an object), kept for the digest.
    Other(Value),
}

impl Content {
    pub fn from_value(v: Value) -> Self {
        match v {
            Value::String(s) => Self::Text(s),
            Value::Array(a) => Self::Blocks(a.into_iter().map(ContentBlock::from_value).collect()),
            other => Self::Other(other),
        }
    }

    pub fn to_value(&self) -> Value {
        match self {
            Self::Text(s) => Value::from(s.as_str()),
            Self::Blocks(b) => Value::Array(b.iter().map(ContentBlock::to_value).collect()),
            Self::Other(v) => v.clone(),
        }
    }

    pub fn blocks(&self) -> Vec<ContentBlock> {
        match self {
            Self::Text(s) => vec![ContentBlock::Text(TextBlock {
                text: s.clone(),
                extra: Map::new(),
            })],
            Self::Blocks(b) => b.clone(),
            Self::Other(_) => Vec::new(),
        }
    }
}

impl Serialize for Content {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        self.to_value().serialize(s)
    }
}

/// One content block. A known `type` whose fields do not fit its variant (e.g. a non-string
/// `text`) is `Other`, so every block re-serializes to exactly what was sent.
#[derive(Debug, Clone, PartialEq)]
pub enum ContentBlock {
    Text(TextBlock),
    ToolUse(ToolUse),
    ToolResult(ToolResult),
    /// The block minus its `type`.
    Image(Map<String, Value>),
    Other(Value),
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct TextBlock {
    pub text: String,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct ToolUse {
    #[serde(
        default,
        deserialize_with = "keep",
        skip_serializing_if = "Option::is_none"
    )]
    pub id: Option<Value>,
    #[serde(
        default,
        deserialize_with = "keep",
        skip_serializing_if = "Option::is_none"
    )]
    pub name: Option<Value>,
    #[serde(
        default,
        deserialize_with = "keep",
        skip_serializing_if = "Option::is_none"
    )]
    pub input: Option<Value>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct ToolResult {
    #[serde(
        default,
        deserialize_with = "keep",
        skip_serializing_if = "Option::is_none"
    )]
    pub tool_use_id: Option<Value>,
    #[serde(
        default,
        deserialize_with = "keep_content",
        skip_serializing_if = "Option::is_none"
    )]
    pub content: Option<Content>,
    #[serde(
        default,
        deserialize_with = "keep",
        skip_serializing_if = "Option::is_none"
    )]
    pub is_error: Option<Value>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl ToolResult {
    /// The result's text: a string content as-is, else its text blocks.
    pub fn text(&self) -> String {
        match &self.content {
            Some(Content::Text(s)) => s.clone(),
            other => protocol::text_of(other.as_ref()),
        }
    }

    pub fn is_error(&self) -> bool {
        crate::session::is_set(self.is_error.as_ref())
    }
}

fn typed<T: DeserializeOwned>(m: &Map<String, Value>) -> Option<T> {
    let mut m = m.clone();
    m.shift_remove("type");
    serde_json::from_value(Value::Object(m)).ok()
}

fn tagged<T: Serialize>(kind: &str, b: &T) -> Value {
    let mut v = serde_json::to_value(b).unwrap_or_else(|_| json!({}));
    if let Value::Object(m) = &mut v {
        m.insert("type".into(), Value::from(kind));
    }
    v
}

impl ContentBlock {
    pub fn from_value(v: Value) -> Self {
        let Value::Object(m) = &v else {
            return Self::Other(v);
        };
        let b = match m.get("type").and_then(Value::as_str) {
            Some("text") => typed(m).map(Self::Text),
            Some("tool_use") => typed(m).map(Self::ToolUse),
            Some("tool_result") => typed(m).map(Self::ToolResult),
            Some("image") => {
                let mut m = m.clone();
                m.shift_remove("type");
                Some(Self::Image(m))
            }
            _ => None,
        };
        match b {
            Some(b) if b.to_value() == v => b,
            _ => Self::Other(v),
        }
    }

    pub fn to_value(&self) -> Value {
        match self {
            Self::Text(b) => tagged("text", b),
            Self::ToolUse(b) => tagged("tool_use", b),
            Self::ToolResult(b) => tagged("tool_result", b),
            Self::Image(m) => tagged("image", m),
            Self::Other(v) => v.clone(),
        }
    }

    /// The block's `type` string, whichever variant carries it.
    pub fn kind(&self) -> Option<&str> {
        match self {
            Self::Text(_) => Some("text"),
            Self::ToolUse(_) => Some("tool_use"),
            Self::ToolResult(_) => Some("tool_result"),
            Self::Image(_) => Some("image"),
            Self::Other(v) => v.get("type").and_then(Value::as_str),
        }
    }
}

impl Request {
    /// The one parse: `messages`, when present, is an array of objects, and every element of
    /// an array `content` is an object; anything else is a 400 before any session effect.
    pub fn parse(body: Value) -> Result<Self, SidecarError> {
        let bad = |m: String| SidecarError::new(400, "invalid_request_error", m);
        match body.get("messages") {
            None | Some(Value::Null) => {}
            Some(Value::Array(msgs)) => {
                for (i, m) in msgs.iter().enumerate() {
                    let Value::Object(o) = m else {
                        return Err(bad(format!("messages[{i}] must be an object")));
                    };
                    if let Some(Value::Array(blocks)) = o.get("content") {
                        if let Some(j) = blocks.iter().position(|b| !b.is_object()) {
                            return Err(bad(format!(
                                "messages[{i}].content[{j}] must be an object"
                            )));
                        }
                    }
                }
            }
            Some(_) => return Err(bad("messages must be an array".into())),
        }
        if !body.is_object() {
            return Ok(Self::default());
        }
        serde_json::from_value(body).map_err(|e| bad(format!("invalid request body: {e}")))
    }

    /// `String(body.model ?? "")`.
    pub fn model_text(&self) -> String {
        match &self.model {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(s)) => s.clone(),
            Some(other) => other.to_string(),
        }
    }

    /// The `tools` array's entries (a non-array `tools` lists none).
    pub fn tool_list(&self) -> &[Value] {
        self.tools
            .as_ref()
            .and_then(Value::as_array)
            .map_or(&[], Vec::as_slice)
    }
}

// --------------------------------------------------------------- digests ----

/// `canon(v)`: object keys sorted, arrays kept, numbers compared by value the way
/// `JSON.stringify` spelled them (`1.0` is `1`, `-0` is `0`, integers past 2^53 round).
pub fn canon(v: &Value) -> Value {
    match v {
        Value::Array(a) => Value::Array(a.iter().map(canon).collect()),
        Value::Object(m) => {
            let mut ks: Vec<&String> = m.keys().collect();
            ks.sort();
            Value::Object(ks.into_iter().map(|k| (k.clone(), canon(&m[k]))).collect())
        }
        Value::Number(n) => match n.as_f64() {
            Some(f) if f.fract() == 0.0 && f.abs() < 9.2e18 => Value::from(f as i64),
            Some(f) => serde_json::Number::from_f64(f).map_or(Value::Null, Value::Number),
            None => Value::Null,
        },
        other => other.clone(),
    }
}

fn canon_digest(v: &Value) -> String {
    protocol::sha256_hex(&canon(v).to_string())
}

/// `undecorateBlock`: drop `cache_control` from an object block.
fn undecorate_block(mut b: Value) -> Value {
    if let Value::Object(m) = &mut b {
        m.shift_remove("cache_control");
    }
    b
}

/// `undecorateMessage`: drop `cache_control`, and `text` blocks after a `tool_result` in a
/// user message.
fn undecorate_message(m: &Message) -> Value {
    let mut v = m.to_value();
    let (Some(Content::Blocks(content)), Value::Object(obj)) = (&m.content, &mut v) else {
        return v;
    };
    let user = m.role() == Some("user");
    let mut after_result = false;
    let mut out = Vec::new();
    for b in content {
        if user && after_result && b.kind() == Some("text") {
            continue;
        }
        if b.kind() == Some("tool_result") {
            after_result = true;
        }
        out.push(undecorate_block(b.to_value()));
    }
    obj.insert("content".into(), Value::Array(out));
    v
}

/// The `requestDigest` input; `None` fields are omitted from the JSON.
#[derive(Debug, Default, Clone)]
pub struct DigestFields<'a> {
    pub model: Option<&'a Value>,
    pub effort: Option<Value>,
    pub system: Option<&'a str>,
    pub tools: Option<&'a Value>,
    pub messages: Option<&'a [Message]>,
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
            Value::Array(a) => Value::Array(a.iter().cloned().map(undecorate_block).collect()),
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
    canon_digest(&Value::Object(m))
}

/// `messageDigests(msgs)`: per-message digests of the undecorated conversation.
pub fn message_digests(msgs: &[Message]) -> Vec<String> {
    msgs.iter()
        .map(|m| canon_digest(&undecorate_message(m)))
        .collect()
}

/// `shortDigest(d)`: the first 12 hex chars.
pub fn short_digest(d: &str) -> String {
    d.chars().take(12).collect()
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

/// The request, read once (`normalizeRequest`).
#[derive(Debug, Clone)]
pub struct Normalized {
    /// The typed request; its `messages` are moved into `msgs` / `other_msgs`.
    pub req: Request,
    pub headers: HeaderMap,
    pub system_text: String,
    /// The conversational (user/assistant) messages, in order.
    pub msgs: Vec<Message>,
    /// Every other entry (interleaved `role: "system"` and unknown roles), in order.
    pub other_msgs: Vec<Message>,
    pub raw_cwd: Option<String>,
    pub interleaved_text: String,
    pub stream: bool,
}

impl Normalized {
    /// The last conversational message's blocks.
    pub fn last_blocks(&self) -> Vec<ContentBlock> {
        self.msgs.last().map(Message::blocks).unwrap_or_default()
    }

    /// The `tool_result` blocks of the last conversational message.
    pub fn tool_results(&self) -> Vec<ToolResult> {
        self.last_blocks()
            .into_iter()
            .filter_map(|b| match b {
                ContentBlock::ToolResult(t) => Some(t),
                _ => None,
            })
            .collect()
    }

    /// Every message, conversational or not (order not preserved across the two).
    pub fn all_msgs(&self) -> impl Iterator<Item = &Message> {
        self.msgs.iter().chain(&self.other_msgs)
    }
}

pub fn normalize_request(mut req: Request, headers: Option<HeaderMap>) -> Normalized {
    let system_text = protocol::system_text_of(&req);
    let (msgs, other_msgs): (Vec<Message>, Vec<Message>) = std::mem::take(&mut req.messages)
        .into_iter()
        .partition(|m| matches!(m.role(), Some("user" | "assistant")));
    Normalized {
        raw_cwd: protocol::extract_cwd(&other_msgs, &system_text),
        interleaved_text: protocol::interleaved_system_text(&other_msgs),
        stream: req.stream,
        headers: headers.unwrap_or_default(),
        req,
        system_text,
        msgs,
        other_msgs,
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
    let model = norm.req.model_text();
    let Some(parsed) = protocol::parse_model_id(&model) else {
        let names: Vec<&str> = MODEL_FAMILIES.iter().map(|(n, _)| *n).collect();
        return Err(SidecarError::new(
            400,
            "invalid_request_error",
            format!(
                "unknown model id \"{model}\" — expected one of {} with an optional -low/-medium/-high suffix",
                names.join(", ")
            ),
        ));
    };
    let eff = protocol::resolve_effort(Some(&parsed), &norm.req);
    let first_user = norm.msgs.iter().find(|m| m.role() == Some("user"));
    let first_text = protocol::text_of(first_user.and_then(|m| m.content.as_ref()));
    let SessionKey { key, weak, scope } = protocol::session_key_of(
        &norm.req,
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
    /// The usable `tool_use_id`s (as `delivered` keys), sorted.
    pub tool_result_ids: Vec<String>,
    pub digest: String,
    /// `{model, effort, system, tools, messages}` short digests (logged on supersede).
    pub parts: Value,
    /// Per-message structural summary (never content), as the log row writes it.
    pub msg_shape: Value,
}

fn effort_value(e: &Effort) -> Value {
    e.effort.as_deref().map_or(Value::Null, Value::from)
}

/// One block's `{type, keys, len}` summary (`keys` is `""` for a non-object block).
fn block_shape(b: &Value) -> Value {
    let mut m = Map::new();
    let mut keys = String::new();
    if let Value::Object(o) = b {
        if let Some(t) = o.get("type") {
            m.insert("type".into(), t.clone());
        }
        let mut ks: Vec<&str> = o.keys().map(String::as_str).collect();
        ks.sort();
        keys = ks.join(",");
    }
    m.insert("keys".into(), Value::from(keys));
    let len = match (b.get("text"), b.get("content")) {
        (Some(Value::String(t)), _) => Value::from(t.chars().count()),
        (_, Some(Value::String(c))) => Value::from(c.chars().count()),
        (_, Some(Value::Array(c))) => Value::from(c.len()),
        _ => Value::Null,
    };
    m.insert("len".into(), len);
    Value::Object(m)
}

/// `requestToken({reqId, mode, norm, ident})`.
pub fn request_token(req_id: String, mode: Mode, norm: &Normalized, ident: &Identity) -> Token {
    let model = norm.req.model.as_ref();
    let empty = Value::Array(Vec::new());
    let tools = norm
        .req
        .tools
        .as_ref()
        .filter(|t| !t.is_null())
        .unwrap_or(&empty);
    let effort = effort_value(&ident.eff);
    let mut ids: Vec<String> = norm
        .tool_results()
        .iter()
        .filter_map(|tr| crate::session::usable_id(tr.tool_use_id.as_ref()))
        .collect();
    ids.sort();
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
        let d = short_digest(&canon_digest(&m.to_value()));
        let blocks: Vec<Value> = match &m.content {
            Some(Content::Blocks(a)) => a.iter().map(|b| block_shape(&b.to_value())).collect(),
            other => {
                let text = match other {
                    Some(Content::Text(s)) => s.clone(),
                    Some(Content::Other(v)) if !v.is_null() => v.to_string(),
                    _ => String::new(),
                };
                vec![block_shape(&json!({ "type": "text", "text": text }))]
            }
        };
        let mut row = Map::new();
        row.insert("d".into(), Value::from(d));
        if let Some(r) = &m.role {
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
    let a = a?.as_array()?;
    let bv = b.filter(|v| !v.is_null())?.as_array();
    let out: Vec<Value> = a
        .iter()
        .enumerate()
        .filter_map(|(i, x)| {
            let y = bv.and_then(|v| v.get(i));
            let differs = y.is_none_or(|y| x.get("d") != y.get("d"));
            differs.then(|| {
                let mut m = Map::new();
                m.insert("i".into(), Value::from(i));
                if let Some(r) = x.get("role") {
                    m.insert("role".into(), r.clone());
                }
                if let Some(r) = x.get("blocks") {
                    m.insert("req".into(), r.clone());
                }
                let owner = y
                    .and_then(|y| y.get("blocks"))
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
                    .all(|id| sess.delivered.contains(id));
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
pub fn renders_transcript(mode: Mode, msgs: &[Message]) -> bool {
    mode.is_new_thread() && msgs.iter().any(|m| m.role() == Some("assistant"))
}

/// A label value as text: a string as-is, a missing or `null` value as `?`.
fn or_q(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "?".into(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

fn render_block(b: &ContentBlock) -> String {
    match b {
        ContentBlock::Text(t) => t.text.clone(),
        ContentBlock::ToolUse(t) => {
            let input = t
                .input
                .as_ref()
                .filter(|v| !v.is_null())
                .cloned()
                .unwrap_or_else(|| json!({}));
            format!(
                "[tool_use {} id={}] {input}",
                or_q(t.name.as_ref()),
                or_q(t.id.as_ref()),
            )
        }
        ContentBlock::ToolResult(t) => {
            let err = if t.is_error() { " is_error" } else { "" };
            format!(
                "[tool_result id={}{err}]\n{}",
                or_q(t.tool_use_id.as_ref()),
                t.text()
            )
        }
        ContentBlock::Image(_) | ContentBlock::Other(_) => String::new(),
    }
}

fn render_blocks(m: Option<&Message>) -> String {
    m.map(Message::blocks)
        .unwrap_or_default()
        .iter()
        .map(render_block)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn chars(s: &str) -> usize {
    s.chars().count()
}

/// Upper bound on [`render_transcript`] output, in chars (conservative against codex's
/// 1 MiB limit), wrapper and elision marker included.
pub const TRANSCRIPT_MAX_CHARS: usize = 240_000;

/// The elision line that opens `<prior-conversation>` when earlier messages were dropped.
pub fn elision_marker(omitted: usize) -> String {
    format!("[{omitted} earlier message(s) omitted: transcript capped at {TRANSCRIPT_MAX_CHARS} characters]")
}

/// `renderTranscript(msgs)`: the conversation, role-labeled, bounded by
/// [`TRANSCRIPT_MAX_CHARS`] chars. Under budget the output is the whole
/// transcript, unchanged. Over budget the wrapper, the elision marker and the final message
/// are reserved first, then prior messages are admitted newest-first into what remains (so
/// they drop oldest-first and keep their order), and the marker states how many were omitted.
/// The final message is never cut: if it alone exceeds the budget, every prior message is
/// elided and the overflow error mapping covers codex's rejection.
pub fn render_transcript(msgs: &[Message]) -> String {
    let prior: Vec<String> = msgs
        .iter()
        .take(msgs.len().saturating_sub(1))
        .map(|m| {
            format!(
                "### {}\n{}",
                m.role().unwrap_or("undefined"),
                render_blocks(Some(m))
            )
        })
        .collect();
    let fin = render_blocks(msgs.last());
    let wrap = |body: &str| {
        format!(
            "<prior-conversation>\nThis thread was (re)started mid-conversation. The transcript so far:\n\n{body}\n</prior-conversation>\n\n{fin}"
        )
    };
    let full = wrap(&prior.join("\n\n"));
    if chars(&full) <= TRANSCRIPT_MAX_CHARS {
        return full;
    }
    // framing + final + the widest marker this request could need
    let reserved = chars(&wrap("")) + chars(&elision_marker(prior.len()));
    let mut room = TRANSCRIPT_MAX_CHARS.saturating_sub(reserved);
    let mut keep = 0;
    for p in prior.iter().rev() {
        let cost = chars(p) + 2; // "\n\n" separator
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
pub fn turn_input_text(mode: Mode, msgs: &[Message]) -> String {
    let text = if renders_transcript(mode, msgs) {
        render_transcript(msgs)
    } else if mode.is_new_thread() {
        msgs.iter()
            .filter(|m| m.role() == Some("user"))
            .map(|m| protocol::text_of(m.content.as_ref()))
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("\n\n")
    } else {
        let last = msgs.last();
        let t = protocol::text_of(last.and_then(|l| l.content.as_ref()));
        if t.trim().is_empty() {
            last.map(Message::blocks)
                .unwrap_or_default()
                .iter()
                .filter_map(|b| match b {
                    ContentBlock::ToolResult(t) => Some(t.text()),
                    _ => None,
                })
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
                .join("\n\n")
        } else {
            t
        }
    };
    if text.trim().is_empty() {
        "(empty user message)".into()
    } else {
        text
    }
}
