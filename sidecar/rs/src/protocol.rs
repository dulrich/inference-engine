//! The Anthropic wire boundary, as pure functions over one request body or one
//! usage figure: model/effort identity, message and system-prompt reading (session-key
//! discriminator, title signature), and the codex -> Anthropic usage projection with its clamp.

use crate::codex::Usage;
use crate::request::{Content, ContentBlock, Message, Request, System};
use axum::http::HeaderMap;
use serde_json::{json, Value};

use sha2::{Digest, Sha256};

pub const MODEL_FAMILIES: &[(&str, &str)] = &[
    ("gpt-5.6-luna", "low"),
    ("gpt-5.6-sol", "medium"),
    ("gpt-6-astra", "medium"),
    ("gpt-6-sol", "medium"),
    ("gpt-6.1-sol", "medium"),
    ("gpt-6.1-terra", "low"),
    ("gpt-6-luna", "low"),
];
/// The harness's `output_config.effort` vocabulary folded onto the app-server's low|medium|high.
pub const EFFORT_LEVELS: &[(&str, &str)] = &[
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("max", "high"),
    ("xhigh", "high"),
];
const TITLE_PHRASE: &str =
    "You are naming a coding session so the user can pick it out of a long list of sessions";
const TITLE_MAX_SYSTEM_CHARS: usize = 4000;
const BILLING_PREFIX: &str = "x-anthropic-billing-header:";
const CWD_PREFIX: &str = "Primary working directory:";

fn lookup(table: &[(&'static str, &'static str)], k: &str) -> Option<&'static str> {
    table.iter().find(|(n, _)| *n == k).map(|(_, v)| *v)
}

/// The characters a regex `.` does not match and `^`/`$` (with `m`) break on.
pub fn is_line_break(c: char) -> bool {
    matches!(c, '\n' | '\r' | '\u{2028}' | '\u{2029}')
}

pub fn sha256_hex(data: &str) -> String {
    Sha256::digest(data.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Where a request's effort came from (logged as `as_str()`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EffortSource {
    Suffix,
    Request,
    Default,
}

impl EffortSource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Suffix => "suffix",
            Self::Request => "request",
            Self::Default => "default",
        }
    }
}

impl From<EffortSource> for Value {
    fn from(v: EffortSource) -> Self {
        Value::from(v.as_str())
    }
}

/// A session key's agent scope (logged as `as_str()`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    Parent,
    Subagent,
}

impl Scope {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Parent => "parent",
            Self::Subagent => "subagent",
        }
    }
}

impl From<Scope> for Value {
    fn from(v: Scope) -> Self {
        Value::from(v.as_str())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedModel {
    pub model: String,
    pub effort: String,
    /// `Suffix` or `Default`.
    pub effort_source: EffortSource,
}

/// A model id: `gpt-5.6-luna`, `codex-a/gpt-5.6-luna`, or either with a
/// `-low|-medium|-high` suffix; anything else `None` (the caller answers 400).
pub fn parse_model_id(raw: &str) -> Option<ParsedModel> {
    let id = raw.trim();
    if id.is_empty() {
        return None;
    }
    let bare = id.rsplit_once('/').map_or(id, |(_, b)| b);
    if let Some(e) = lookup(MODEL_FAMILIES, bare) {
        return Some(ParsedModel {
            model: bare.into(),
            effort: e.into(),
            effort_source: EffortSource::Default,
        });
    }
    // /^(.*)-(low|medium|high)$/ — `.` excludes line terminators
    for suf in ["low", "medium", "high"] {
        if let Some(head) = bare.strip_suffix(suf).and_then(|h| h.strip_suffix('-')) {
            if !head.contains(is_line_break) && lookup(MODEL_FAMILIES, head).is_some() {
                return Some(ParsedModel {
                    model: head.into(),
                    effort: suf.into(),
                    effort_source: EffortSource::Suffix,
                });
            }
        }
    }
    None
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Effort {
    pub effort: Option<String>,
    pub effort_source: EffortSource,
}

/// The effort: suffix -> `output_config.effort` -> `thinking.type ===
/// "disabled"` (low) -> family default. Never an error.
pub fn resolve_effort(parsed: Option<&ParsedModel>, req: &Request) -> Effort {
    if let Some(p) = parsed.filter(|p| p.effort_source == EffortSource::Suffix) {
        return Effort {
            effort: Some(p.effort.clone()),
            effort_source: EffortSource::Suffix,
        };
    }
    let want = req.output_config.effort.as_deref();
    if let Some(e) = want.and_then(|w| lookup(EFFORT_LEVELS, w)) {
        return Effort {
            effort: Some(e.into()),
            effort_source: EffortSource::Request,
        };
    }
    if req.thinking.kind.as_deref() == Some("disabled") {
        return Effort {
            effort: Some("low".into()),
            effort_source: EffortSource::Request,
        };
    }
    Effort {
        effort: parsed.map(|p| p.effort.clone()),
        effort_source: EffortSource::Default,
    }
}

/// A content's text: only text blocks survive, joined by `\n`.
pub fn text_of(content: Option<&Content>) -> String {
    content
        .map(Content::blocks)
        .unwrap_or_default()
        .iter()
        .filter_map(|b| match b {
            ContentBlock::Text(t) => Some(t.text.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// `String(x.text ?? "")` for a `system` block.
fn system_block_text(b: &Value) -> String {
    match b {
        Value::String(s) => s.clone(),
        other => match other.get("text") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(s)) => s.clone(),
            Some(v) => v.to_string(),
        },
    }
}

/// The request's `system` as one text.
pub fn system_text_of(req: &Request) -> String {
    match &req.system {
        Some(System::Text(s)) => s.clone(),
        Some(System::Blocks(a)) => a
            .iter()
            .map(system_block_text)
            .collect::<Vec<_>>()
            .join("\n\n"),
        None => String::new(),
    }
}

/// The text of interleaved role:"system" entries, in order.
pub fn interleaved_system_text(msgs: &[Message]) -> String {
    msgs.iter()
        .filter(|m| m.role() == Some("system"))
        .map(|m| text_of(m.content.as_ref()).trim().to_string())
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n")
}

/// The thread's `baseInstructions`: preamble, then the system text and the interleaved
/// system text — `preamble` is `Pinned::preamble`.
pub fn base_instructions_of(preamble: &str, system_text: &str, interleaved: &str) -> String {
    let head = if system_text.is_empty() {
        "You are a coding assistant driven by an external harness."
    } else {
        system_text
    };
    [preamble, head, interleaved]
        .into_iter()
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n")
}

/// Whether the request carries an image block in any message (or in a `tool_result`), or in
/// `system`.
pub fn has_image_block<'a>(req: &Request, msgs: impl IntoIterator<Item = &'a Message>) -> bool {
    for m in msgs {
        for b in m.blocks() {
            match &b {
                ContentBlock::Image(_) => return true,
                ContentBlock::ToolResult(t)
                    if t.content.as_ref().is_some_and(|c| {
                        c.blocks()
                            .iter()
                            .any(|x| matches!(x, ContentBlock::Image(_)))
                    }) =>
                {
                    return true
                }
                _ => {}
            }
        }
    }
    matches!(&req.system, Some(System::Blocks(a)) if a.iter().any(|b| b.get("type") == Some(&Value::from("image"))))
}

/// `CWD_RE.exec(text)?.[1]` for `/^[ \t]*Primary working directory:[ \t]*(.+?)[ \t]*$/m`.
fn cwd_in(text: &str) -> Option<String> {
    let blank = |c: char| c == ' ' || c == '\t';
    for line in text.split(is_line_break) {
        let Some(rest) = line.trim_start_matches(blank).strip_prefix(CWD_PREFIX) else {
            continue;
        };
        let inner = rest.trim_matches(blank);
        if !inner.is_empty() {
            return Some(inner.to_string());
        }
        // all-blank remainder: the lazy group backtracks onto its last blank char
        if let Some(c) = rest.chars().last() {
            return Some(c.to_string());
        }
    }
    None
}

/// The client's cwd, parsed before the system-role filter.
pub fn extract_cwd(msgs: &[Message], system_text: &str) -> Option<String> {
    for m in msgs.iter().filter(|m| m.role() == Some("system")) {
        if let Some(c) = cwd_in(&text_of(m.content.as_ref())) {
            return Some(c);
        }
    }
    cwd_in(system_text)
}

/// The `x-anthropic-billing-header:` first line of `system`, or `""`.
pub fn billing_line_of(req: &Request) -> String {
    let head = match &req.system {
        Some(System::Text(s)) => s.as_str(),
        Some(System::Blocks(a)) => match a.first() {
            Some(Value::String(s)) => s.as_str(),
            f => f
                .and_then(|f| f.get("text"))
                .and_then(Value::as_str)
                .unwrap_or(""),
        },
        None => "",
    };
    let line = head.split('\n').next().unwrap_or("");
    if line.trim_start().to_lowercase().starts_with(BILLING_PREFIX) {
        line.to_string()
    } else {
        String::new()
    }
}

/// `/(?:^|[\s;])cc_is_subagent=true(?:[;\s]|$)/` on the billing line.
pub fn is_subagent_request(req: &Request) -> bool {
    const TOKEN: &str = "cc_is_subagent=true";
    let line = billing_line_of(req);
    line.match_indices(TOKEN).any(|(i, _)| {
        let before = line[..i].chars().next_back();
        let after = line[i + TOKEN.len()..].chars().next();
        before.is_none_or(|c| c == ';' || c.is_whitespace())
            && after.is_none_or(|c| c == ';' || c.is_whitespace())
    })
}

/// The system text with the leading billing line removed.
pub fn system_text_without_billing_line(req: &Request, system_text: &str) -> String {
    let line = billing_line_of(req);
    if line.is_empty() || !system_text.starts_with(&line) {
        return system_text.to_string();
    }
    system_text[line.len()..]
        .trim_start_matches('\n')
        .to_string()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionKey {
    pub key: String,
    pub weak: bool,
    pub scope: Scope,
}

/// The session key: one thread per (session_id,
/// agent scope); a missing session id falls back to a weak prompt hash.
pub fn session_key_of(
    req: &Request,
    system_text: &str,
    first_user_text: &str,
    headers: Option<&HeaderMap>,
) -> SessionKey {
    let parsed: Option<Value> = match &req.metadata.user_id {
        Some(Value::String(s)) => serde_json::from_str(s).ok(),
        Some(v @ (Value::Object(_) | Value::Array(_))) => Some(v.clone()),
        _ => None,
    };
    let sid = parsed
        .as_ref()
        .and_then(|p| p.get("session_id"))
        .and_then(Value::as_str)
        .map(|s| s.trim().to_string())
        .unwrap_or_default();
    let scope = if is_subagent_request(req) {
        Scope::Subagent
    } else {
        Scope::Parent
    };
    if !sid.is_empty() {
        if scope == Scope::Parent {
            return SessionKey {
                key: sid,
                weak: false,
                scope,
            };
        }
        let agent = headers
            .and_then(|h| h.get("x-claude-code-agent-id"))
            .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned())
            .unwrap_or_default();
        let agent = agent.trim();
        if !agent.is_empty() {
            return SessionKey {
                key: format!("{sid}:{agent}"),
                weak: false,
                scope,
            };
        }
        let disc = sha256_hex(&format!(
            "{}\0{first_user_text}",
            system_text_without_billing_line(req, system_text)
        ));
        return SessionKey {
            key: format!("{sid}:{}", &disc[..16]),
            weak: false,
            scope,
        };
    }
    let hash = sha256_hex(&format!("{system_text}\0{first_user_text}"));
    SessionKey {
        key: hash[..32].to_string(),
        weak: true,
        scope,
    }
}

/// A session-title query: empty tools, short system carrying the naming
/// phrase, a single user message.
pub fn is_title_request(req: &Request, system_text: &str, msgs: &[Message]) -> bool {
    if !req.tool_list().is_empty() {
        return false;
    }
    if system_text.is_empty() || system_text.chars().count() >= TITLE_MAX_SYSTEM_CHARS {
        return false;
    }
    if !system_text.contains(TITLE_PHRASE) {
        return false;
    }
    msgs.len() == 1 && msgs[0].role() == Some("user")
}

/// The title envelope label: `"pinned"` when the user message opens with a `<session>` block.
pub fn title_envelope(msgs: &[Message]) -> &'static str {
    let t = text_of(msgs.first().and_then(|m| m.content.as_ref()));
    let pinned = t
        .trim_start()
        .strip_prefix("<session>")
        .is_some_and(|r| r.contains("</session>"));
    if pinned {
        "pinned"
    } else {
        "drifted"
    }
}

/// A tool's `input_schema` (or `inputSchema`), when set.
fn schema_of(t: &Value) -> Option<&Value> {
    ["input_schema", "inputSchema"]
        .into_iter()
        .find_map(|k| t.get(k).filter(|v| !v.is_null()))
}

/// The `count_tokens` estimate: chars / 4, rounded up, over the system text, every message
/// block and the tool specs.
pub fn count_tokens_estimate<'a>(
    req: &Request,
    msgs: impl IntoIterator<Item = &'a Message>,
) -> u64 {
    let n = |s: &str| s.chars().count();
    let mut chars = n(&system_text_of(req));
    for m in msgs {
        for b in m.blocks() {
            let v = b.to_value();
            let content = v.get("content").filter(|c| !c.is_null());
            if let Some(Value::String(t)) = v.get("text") {
                chars += n(t);
            } else if let Some(Value::String(c)) = content {
                chars += n(c);
            } else if let Some(x) = content.or_else(|| v.get("input").filter(|i| !i.is_null())) {
                chars += n(&x.to_string());
            }
        }
    }
    for t in req.tool_list() {
        let text = |k: &str| match t.get(k) {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(s)) => s.clone(),
            Some(v) => v.to_string(),
        };
        let schema = schema_of(t).cloned().unwrap_or_else(|| json!({}));
        chars += n(&text("name")) + n(&text("description")) + n(&schema.to_string());
    }
    chars.div_ceil(4) as u64
}

/// Key comparison: constant-time over equal lengths; empty never matches.
pub fn safe_equal(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.is_empty() || b.is_empty() || a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// The key a request presents: `authorization` (`Bearer` optional), else `x-api-key`.
pub fn presented_key(headers: &HeaderMap) -> Option<String> {
    let text = |k: &str| {
        headers
            .get(k)
            .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned())
    };
    if let Some(a) = text("authorization") {
        let a = a.trim();
        if !a.is_empty() {
            // /^bearer\s+(.*)$/i
            let tail = a
                .get(..6)
                .filter(|h| h.eq_ignore_ascii_case("bearer"))
                .map(|_| &a[6..])
                .filter(|r| r.starts_with(char::is_whitespace))
                .map(str::trim_start)
                .filter(|r| !r.contains(is_line_break));
            return Some(tail.unwrap_or(a).trim().to_string());
        }
    }
    if let Some(x) = text("x-api-key") {
        let x = x.trim();
        if !x.is_empty() {
            return Some(x.to_string());
        }
    }
    None
}

/// Anthropic tools -> app-server DynamicToolSpec (function variant);
/// the description is cut at 4000 chars.
pub fn to_dynamic_tools(tools: &[Value]) -> Vec<Value> {
    tools
        .iter()
        .filter_map(|t| {
            let name = t
                .get("name")
                .and_then(Value::as_str)
                .filter(|n| !n.is_empty())?;
            let desc = match t.get("description") {
                Some(Value::String(d)) if !d.is_empty() => d.as_str(),
                _ => name,
            };
            let desc: String = desc.chars().take(4000).collect();
            let schema = schema_of(t)
                .cloned()
                .unwrap_or_else(|| json!({ "type": "object", "properties": {} }));
            Some(json!({
                "type": "function",
                "name": name,
                "description": desc,
                "inputSchema": schema,
            }))
        })
        .collect()
}

/// Codex usage -> Anthropic usage; `input_tokens` is the uncached share,
/// clamped at 0.
pub fn usage_out(u: Option<&Usage>) -> Value {
    let u = u.copied().unwrap_or_default();
    json!({
        "input_tokens": u.input.saturating_sub(u.cached).saturating_sub(u.cache_write),
        "cache_creation_input_tokens": u.cache_write,
        "cache_read_input_tokens": u.cached,
        "output_tokens": u.output,
    })
}

/// The usage clamp: `None` when nothing needs clamping, else the RAW-units figure
/// whose projection's three input columns sum to exactly `window`.
pub fn clamp_usage(raw: &Usage, window: u64) -> Option<Usage> {
    if window == 0 {
        return None;
    }
    let vals = [
        raw.input
            .saturating_sub(raw.cached)
            .saturating_sub(raw.cache_write),
        raw.cached,
        raw.cache_write,
    ];
    let sum: u128 = vals.iter().map(|v| u128::from(*v)).sum();
    if sum <= u128::from(window) {
        return None;
    }
    let mut scaled: Vec<u64> = vals
        .iter()
        .map(|v| (u128::from(*v) * u128::from(window) / sum) as u64)
        .collect();
    let mut rest = window - scaled.iter().sum::<u64>();
    // the remainder goes to the largest columns first (stable on ties)
    let mut order: Vec<usize> = (0..vals.len()).collect();
    order.sort_by(|&a, &b| vals[b].cmp(&vals[a]));
    for i in order {
        if rest == 0 {
            break;
        }
        scaled[i] += 1;
        rest -= 1;
    }
    let (input, cache_read, cache_write) = (scaled[0], scaled[1], scaled[2]);
    Some(Usage {
        input: input + cache_read + cache_write,
        cached: cache_read,
        cache_write,
        output: raw.output,
    })
}
