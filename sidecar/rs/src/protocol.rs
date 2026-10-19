//! protocol.mjs — the Anthropic wire boundary, as pure functions over one request body or one
//! usage figure: model/effort identity, message and system-prompt reading (session-key
//! discriminator, title signature), and the codex -> Anthropic usage projection with its clamp.

use crate::codex::Usage;
use crate::js;
use axum::http::HeaderMap;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

pub const MODEL_FAMILIES: &[(&str, &str)] = &[
    ("gpt-5.6-luna", "low"),
    ("gpt-5.6-sol", "medium"),
    ("gpt-6-astra", "medium"),
    ("gpt-6-sol", "medium"),
    ("gpt-6.1-sol", "medium"),
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

/// `parseModelId(raw)`: `gpt-5.6-luna`, `codex-a/gpt-5.6-luna`, or either with a
/// `-low|-medium|-high` suffix; anything else `None` (the caller answers 400).
pub fn parse_model_id(raw: Option<&Value>) -> Option<ParsedModel> {
    let s = js::js_string_or_empty(raw);
    let id = js::trim(&s);
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
            if !head.contains(js::is_line_terminator) && lookup(MODEL_FAMILIES, head).is_some() {
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

/// `resolveEffort(parsed, body)`: suffix -> `output_config.effort` -> `thinking.type ===
/// "disabled"` (low) -> family default. Never an error.
pub fn resolve_effort(parsed: Option<&ParsedModel>, body: &Value) -> Effort {
    if let Some(p) = parsed.filter(|p| p.effort_source == EffortSource::Suffix) {
        return Effort {
            effort: Some(p.effort.clone()),
            effort_source: EffortSource::Suffix,
        };
    }
    let want = js::prop(js::prop(Some(body), "output_config"), "effort").and_then(Value::as_str);
    if let Some(e) = want.and_then(|w| lookup(EFFORT_LEVELS, w)) {
        return Effort {
            effort: Some(e.into()),
            effort_source: EffortSource::Request,
        };
    }
    if js::prop(js::prop(Some(body), "thinking"), "type") == Some(&Value::from("disabled")) {
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

/// `blocksOf(content)`: an array as-is, a string as one text block, else none.
pub fn blocks_of(content: Option<&Value>) -> Vec<Value> {
    match content {
        Some(Value::Array(a)) => a.clone(),
        Some(Value::String(s)) => vec![json!({ "type": "text", "text": s })],
        _ => Vec::new(),
    }
}

/// `textOf(content)`: only text blocks survive, joined by `\n`.
pub fn text_of(content: Option<&Value>) -> String {
    blocks_of(content)
        .iter()
        .filter(|b| js::prop(Some(b), "type") == Some(&Value::from("text")))
        .filter_map(|b| {
            js::prop(Some(b), "text")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// `systemTextOf(body)`.
pub fn system_text_of(body: &Value) -> String {
    match js::prop(Some(body), "system") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(a)) => a
            .iter()
            .map(|x| match x {
                Value::String(s) => s.clone(),
                other => js::js_string_or_empty(js::prop(Some(other), "text")),
            })
            .collect::<Vec<_>>()
            .join("\n\n"),
        _ => String::new(),
    }
}

fn role_is(m: &Value, role: &str) -> bool {
    js::prop(Some(m), "role") == Some(&Value::from(role))
}

/// `conversational(msgs)`: only user/assistant entries are turns.
pub fn conversational(msgs: Option<&Value>) -> Vec<Value> {
    msgs.and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|m| role_is(m, "user") || role_is(m, "assistant"))
        .cloned()
        .collect()
}

/// `interleavedSystemText(msgs)`: the text of interleaved role:"system" entries, in order.
pub fn interleaved_system_text(msgs: &[Value]) -> String {
    msgs.iter()
        .filter(|m| role_is(m, "system"))
        .map(|m| js::trim(&text_of(js::prop(Some(m), "content"))).to_string())
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n")
}

/// `baseInstructionsOf(systemText, interleaved)` — `preamble` is `Pinned::preamble`.
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

fn type_is(b: &Value, t: &str) -> bool {
    js::prop(Some(b), "type") == Some(&Value::from(t))
}

/// `hasImageBlock(body)`.
pub fn has_image_block(body: &Value) -> bool {
    for m in js::prop(Some(body), "messages")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        for b in blocks_of(js::prop(Some(m), "content")) {
            if type_is(&b, "image") {
                return true;
            }
            if type_is(&b, "tool_result")
                && blocks_of(js::prop(Some(&b), "content"))
                    .iter()
                    .any(|c| type_is(c, "image"))
            {
                return true;
            }
        }
    }
    js::prop(Some(body), "system")
        .and_then(Value::as_array)
        .is_some_and(|a| a.iter().any(|b| type_is(b, "image")))
}

/// `CWD_RE.exec(text)?.[1]` for `/^[ \t]*Primary working directory:[ \t]*(.+?)[ \t]*$/m`.
fn cwd_in(text: &str) -> Option<String> {
    let blank = |c: char| c == ' ' || c == '\t';
    for line in text.split(js::is_line_terminator) {
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

/// `extractCwd(allMsgs, systemText)`: the cwd, parsed before the system-role filter.
pub fn extract_cwd(all_msgs: &[Value], system_text: &str) -> Option<String> {
    for m in all_msgs.iter().filter(|m| role_is(m, "system")) {
        if let Some(c) = cwd_in(&text_of(js::prop(Some(m), "content"))) {
            return Some(c);
        }
    }
    cwd_in(system_text)
}

/// `billingLineOf(body)`: the `x-anthropic-billing-header:` first line of `system`, or `""`.
pub fn billing_line_of(body: &Value) -> String {
    let head = match js::prop(Some(body), "system") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(a)) => match a.first() {
            Some(Value::String(s)) => s.clone(),
            f => match js::prop(f, "text") {
                Some(Value::String(s)) => s.clone(),
                _ => String::new(),
            },
        },
        _ => String::new(),
    };
    let line = head.split('\n').next().unwrap_or("");
    if js::trim_start(line)
        .to_lowercase()
        .starts_with(BILLING_PREFIX)
    {
        line.to_string()
    } else {
        String::new()
    }
}

/// `/(?:^|[\s;])cc_is_subagent=true(?:[;\s]|$)/` on the billing line.
pub fn is_subagent_request(body: &Value) -> bool {
    const TOKEN: &str = "cc_is_subagent=true";
    let line = billing_line_of(body);
    line.match_indices(TOKEN).any(|(i, _)| {
        let before = line[..i].chars().next_back();
        let after = line[i + TOKEN.len()..].chars().next();
        before.is_none_or(|c| c == ';' || js::is_js_space(c))
            && after.is_none_or(|c| c == ';' || js::is_js_space(c))
    })
}

/// `systemTextWithoutBillingLine(body, systemText)`.
pub fn system_text_without_billing_line(body: &Value, system_text: &str) -> String {
    let line = billing_line_of(body);
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

/// `sessionKeyOf(body, systemText, firstUserText, headers)`: one thread per (session_id,
/// agent scope); a missing session id falls back to a weak prompt hash.
pub fn session_key_of(
    body: &Value,
    system_text: &str,
    first_user_text: &str,
    headers: Option<&HeaderMap>,
) -> SessionKey {
    let uid = js::prop(js::prop(Some(body), "metadata"), "user_id");
    let parsed: Option<Value> = match uid {
        Some(Value::String(s)) => serde_json::from_str(s).ok(),
        Some(v @ (Value::Object(_) | Value::Array(_))) => Some(v.clone()),
        _ => None,
    };
    let sid = js::prop(parsed.as_ref(), "session_id")
        .and_then(Value::as_str)
        .map(|s| js::trim(s).to_string())
        .unwrap_or_default();
    let scope = if is_subagent_request(body) {
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
        let agent = js::trim(&agent);
        if !agent.is_empty() {
            return SessionKey {
                key: format!("{sid}:{agent}"),
                weak: false,
                scope,
            };
        }
        let disc = sha256_hex(&format!(
            "{}\0{first_user_text}",
            system_text_without_billing_line(body, system_text)
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

/// `isTitleRequest(body, systemText, msgs)`: empty tools, short system carrying the naming
/// phrase, a single user message.
pub fn is_title_request(body: &Value, system_text: &str, msgs: &[Value]) -> bool {
    let tools = js::prop(Some(body), "tools")
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    if tools != 0 {
        return false;
    }
    if system_text.is_empty() || js::utf16_len(system_text) >= TITLE_MAX_SYSTEM_CHARS {
        return false;
    }
    if !system_text.contains(TITLE_PHRASE) {
        return false;
    }
    msgs.len() == 1 && role_is(&msgs[0], "user")
}

/// `titleEnvelope(msgs)`: `"pinned"` when the user message opens with a `<session>` block.
pub fn title_envelope(msgs: &[Value]) -> &'static str {
    let t = text_of(msgs.first().and_then(|m| js::prop(Some(m), "content")));
    let pinned = js::trim_start(&t)
        .strip_prefix("<session>")
        .is_some_and(|r| r.contains("</session>"));
    if pinned {
        "pinned"
    } else {
        "drifted"
    }
}

/// `countTokensEstimate(body)`: UTF-16 chars / 4, rounded up.
pub fn count_tokens_estimate(body: &Value) -> f64 {
    let mut chars = js::utf16_len(&system_text_of(body));
    for m in js::prop(Some(body), "messages")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        for b in blocks_of(js::prop(Some(m), "content")) {
            let content = js::prop(Some(&b), "content");
            if let Some(Value::String(t)) = js::prop(Some(&b), "text") {
                chars += js::utf16_len(t);
            } else if let Some(Value::String(c)) = content {
                chars += js::utf16_len(c);
            } else if js::truthy(content) || js::truthy(js::prop(Some(&b), "input")) {
                let v = js::present(content).or_else(|| js::prop(Some(&b), "input"));
                chars += v.map_or(0, |v| js::utf16_len(&js::stringify(v)));
            }
        }
    }
    for t in js::prop(Some(body), "tools")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let schema = js::present(js::prop(Some(t), "input_schema"))
            .or_else(|| js::present(js::prop(Some(t), "inputSchema")))
            .cloned()
            .unwrap_or_else(|| json!({}));
        chars += js::utf16_len(&js::js_string_or_empty(js::prop(Some(t), "name")))
            + js::utf16_len(&js::js_string_or_empty(js::prop(Some(t), "description")))
            + js::utf16_len(&js::stringify(&schema));
    }
    (chars as f64 / 4.0).ceil()
}

/// `safeEqual(a, b)`: constant-time over equal lengths; empty never matches.
pub fn safe_equal(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.is_empty() || b.is_empty() || a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// `presentedKey(headers)`: `authorization` (`Bearer` optional), else `x-api-key`.
pub fn presented_key(headers: &HeaderMap) -> Option<String> {
    let text = |k: &str| {
        headers
            .get(k)
            .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned())
    };
    if let Some(a) = text("authorization") {
        let a = a.as_str();
        let a = js::trim(a);
        if !a.is_empty() {
            // /^bearer\s+(.*)$/i
            let tail = a
                .get(..6)
                .filter(|h| h.eq_ignore_ascii_case("bearer"))
                .map(|_| &a[6..])
                .filter(|r| r.starts_with(js::is_js_space))
                .map(|r| r.trim_start_matches(js::is_js_space))
                .filter(|r| !r.contains(js::is_line_terminator));
            return Some(js::trim(tail.unwrap_or(a)).to_string());
        }
    }
    if let Some(x) = text("x-api-key") {
        let x = js::trim(&x);
        if !x.is_empty() {
            return Some(x.to_string());
        }
    }
    None
}

/// `toDynamicTools(tools)`: Anthropic tools -> app-server DynamicToolSpec (function variant).
pub fn to_dynamic_tools(tools: Option<&Value>) -> Vec<Value> {
    tools
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|t| {
            let name = js::prop(Some(t), "name")
                .and_then(Value::as_str)
                .filter(|n| !n.is_empty())?;
            let d = js::prop(Some(t), "description");
            let desc = if js::truthy(d) {
                js::js_string(d.unwrap_or(&Value::Null))
            } else {
                name.to_string()
            };
            let schema = js::present(js::prop(Some(t), "input_schema"))
                .or_else(|| js::present(js::prop(Some(t), "inputSchema")))
                .cloned()
                .unwrap_or_else(|| json!({ "type": "object", "properties": {} }));
            Some(json!({
                "type": "function",
                "name": name,
                "description": js::utf16_prefix(&desc, 4000),
                "inputSchema": schema,
            }))
        })
        .collect()
}

/// `usageOut(u)`: codex usage -> Anthropic usage; `input_tokens` is the uncached share,
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

/// `clampUsage(raw, window)`: `None` when nothing needs clamping, else the RAW-units figure
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
