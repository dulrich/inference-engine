//! The sidecar's error taxonomy and the upstream -> Anthropic mapping.
//!
//! One owner for "what does the client see when something fails": the `SidecarError` value,
//! the CodexErrorInfo/TurnError translation, and the rate-limit reading that turns an account
//! snapshot into a 429 with a retry-after. No HTTP, session or child process here.

use serde_json::{json, Value};
use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub struct SidecarError {
    pub status: u16,
    pub anthropic_type: String,
    pub message: String,
    /// Seconds for the `retry-after` header (a codex account rate-limit exhaustion).
    pub retry_after: Option<u64>,
    /// The original codex text of a rewritten error (logged as `upstreamMessage`).
    pub upstream_message: Option<String>,
    /// A codex context overflow (`contextWindowExceeded` or the app-server's `Input exceeds
    /// the maximum length` rejection). The message still holds the original codex text; the
    /// request layer, which knows the window and the request size, rewrites it with
    /// [`prompt_too_long`] before it reaches the wire.
    pub context_overflow: bool,
}

impl SidecarError {
    pub fn new(status: u16, anthropic_type: &str, message: impl Into<String>) -> Self {
        Self {
            status,
            anthropic_type: anthropic_type.into(),
            message: message.into(),
            retry_after: None,
            upstream_message: None,
            context_overflow: false,
        }
    }

    /// `{type:"error", error:{type, message}}` — the Anthropic error body.
    pub fn body(&self) -> Value {
        json!({ "type": "error", "error": { "type": self.anthropic_type, "message": self.message } })
    }
}

impl SidecarError {
    fn rewritten(self, message: String) -> Self {
        Self {
            status: 400,
            anthropic_type: "invalid_request_error".into(),
            message,
            ..self
        }
    }
}

impl fmt::Display for SidecarError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for SidecarError {}

/// Any failure that is not already a `SidecarError` becomes a 502 `api_error` carrying its
/// message (`"sidecar failure"` when the message is empty).
pub fn as_sidecar_error(message: &str) -> SidecarError {
    let m = if message.is_empty() {
        "sidecar failure"
    } else {
        message
    };
    SidecarError::new(502, "api_error", m)
}

/// CodexErrorInfo string variants (schema) -> Anthropic error shape.
const CODEX_ERROR_MAP: &[(&str, u16, &str)] = &[
    ("unauthorized", 401, "authentication_error"),
    ("rateLimitExceeded", 429, "rate_limit_error"),
    ("usageLimitExceeded", 429, "rate_limit_error"),
    ("sessionBudgetExceeded", 429, "rate_limit_error"),
    ("serverOverloaded", 502, "api_error"),
];

fn context_overflow(full: String) -> SidecarError {
    SidecarError {
        context_overflow: true,
        ..SidecarError::new(400, "invalid_request_error", full)
    }
}

impl SidecarError {
    pub fn is_context_overflow(&self) -> bool {
        self.context_overflow
    }
}

/// Rewrite a context-overflow error into Claude Code's "prompt is too long" shape:
/// `prompt is too long: N tokens > M maximum`, with N floored at M+1 so N > M always holds.
/// The original codex text is kept in `upstream_message` for the `http-error` row.
pub fn prompt_too_long(e: &SidecarError, estimate: f64, window: f64) -> SidecarError {
    let m = if window.is_finite() && window >= 0.0 {
        window.floor()
    } else {
        0.0
    };
    let est = if estimate.is_finite() {
        estimate.ceil()
    } else {
        0.0
    };
    let n = est.max(m + 1.0);
    SidecarError {
        upstream_message: Some(e.message.clone()),
        ..e.clone()
    }
    .rewritten(format!("prompt is too long: {n} tokens > {m} maximum"))
}

/// `/rate.?limit|usage limit|quota|too many requests/i`
fn is_rate_limit_text(lower: &str) -> bool {
    lower.contains("usage limit")
        || lower.contains("quota")
        || lower.contains("too many requests")
        || has_gap(lower, "rate", "limit", Some(1))
}

/// `/unauthori[sz]ed|authentication|not logged in|login required|credential|token refresh|invalid.*api.?key/i`
fn is_auth_text(lower: &str) -> bool {
    [
        "unauthorised",
        "unauthorized",
        "authentication",
        "not logged in",
        "login required",
        "credential",
        "token refresh",
    ]
    .iter()
    .any(|p| lower.contains(p))
        || invalid_api_key(lower)
}

/// `a` then at most `max` (None = any number of) non-line-terminator chars then `b`.
fn has_gap(s: &str, a: &str, b: &str, max: Option<usize>) -> bool {
    s.match_indices(a).any(|(i, _)| {
        let rest = &s[i + a.len()..];
        for (gap, (j, c)) in rest.char_indices().enumerate() {
            if rest[j..].starts_with(b) {
                return true;
            }
            if crate::protocol::is_line_break(c) || max.is_some_and(|m| gap >= m) {
                return false;
            }
        }
        false
    })
}

fn invalid_api_key(s: &str) -> bool {
    s.match_indices("invalid").any(|(i, _)| {
        let line = s[i..]
            .split(crate::protocol::is_line_break)
            .next()
            .unwrap_or("");
        line.match_indices("api")
            .any(|(j, _)| has_gap(&line[j..], "api", "key", Some(1)))
    })
}

/// ASCII-only case folding: the match never folds non-ASCII onto ASCII.
fn ascii_lower(s: &str) -> String {
    s.chars().map(|c| c.to_ascii_lowercase()).collect()
}

/// An app-server JSON-RPC error / TurnError
/// onto an Anthropic error shape.
pub fn map_upstream_error(err: &Value, ctx_label: &str) -> SidecarError {
    let obj = err.as_object();
    let text = match err {
        Value::String(s) => s.clone(),
        Value::Null => "{}".into(),
        other => other.to_string(),
    };
    let get = |k: &str| obj.and_then(|o| o.get(k)).filter(|x| !x.is_null());
    let nested = |outer: &str, k: &str| get(outer).and_then(|o| o.get(k)).filter(|x| !x.is_null());
    let msg = get("message")
        .or_else(|| nested("error", "message"))
        .map_or_else(
            || text.clone(),
            |m| match m {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            },
        );
    let info = get("codexErrorInfo")
        .or_else(|| nested("error", "codexErrorInfo"))
        .or_else(|| nested("data", "codexErrorInfo"));
    // a string variant, or an object variant's (first) key
    let info_key: Option<String> = match info {
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Object(m)) => m.keys().next().cloned(),
        Some(Value::Array(a)) if !a.is_empty() => Some("0".into()),
        _ => None,
    };
    let full = format!("{ctx_label}: {msg}");
    if info_key.as_deref() == Some("contextWindowExceeded")
        || text.contains("Input exceeds the maximum length")
        || msg.contains("Input exceeds the maximum length")
    {
        return context_overflow(full);
    }
    if let Some(k) = &info_key {
        if let Some((_, status, ty)) = CODEX_ERROR_MAP.iter().find(|(n, _, _)| n == k) {
            return SidecarError::new(*status, ty, full);
        }
    }
    // object CodexErrorInfo variants forward the upstream HTTP status verbatim
    let http_status = match (&info_key, info) {
        (Some(k), Some(Value::Object(m))) => m
            .get(k)
            .and_then(|x| x.get("httpStatusCode"))
            .and_then(Value::as_f64),
        (Some(_), Some(Value::Array(a))) => a
            .first()
            .and_then(|x| x.get("httpStatusCode"))
            .and_then(Value::as_f64),
        _ => None,
    };
    if http_status == Some(401.0) || http_status == Some(403.0) {
        return SidecarError::new(401, "authentication_error", full);
    }
    if http_status == Some(429.0) {
        return SidecarError::new(429, "rate_limit_error", full);
    }
    let lower = ascii_lower(&text);
    if is_rate_limit_text(&lower) {
        return SidecarError::new(429, "rate_limit_error", full);
    }
    if is_auth_text(&lower) {
        return SidecarError::new(401, "authentication_error", full);
    }
    SidecarError::new(502, "api_error", full)
}

/// Seconds until a rate-limit window resets: RateLimitWindow.resetsAt is an absolute int64; tolerate
/// s or ms epochs. A past reset still asks for one second.
pub fn retry_after_seconds(resets_at: Option<&Value>, now_ms: f64) -> Option<f64> {
    let r = resets_at?.as_f64().filter(|f| f.is_finite())?;
    let ms = if r > 1e12 { r } else { r * 1000.0 };
    let secs = ((ms - now_ms) / 1000.0).ceil();
    Some(if secs > 0.0 { secs } else { 1.0 })
}

#[derive(Debug, Clone, PartialEq)]
pub struct Exhaustion {
    pub reason: Value,
    pub retry_after: Option<f64>,
}

/// A rate-limit snapshot is exhausted when a window is at/over 100 % or a reached-type
/// is set.
pub fn rate_limit_exhaustion(snapshot: Option<&Value>, now_ms: f64) -> Option<Exhaustion> {
    let s = snapshot.filter(|v| v.is_object() || v.is_array())?;
    let windows: Vec<&Value> = ["primary", "secondary"]
        .iter()
        .filter_map(|k| s.get(*k))
        .filter(|w| w.is_object() || w.is_array())
        .collect();
    let full: Vec<&Value> = windows
        .iter()
        .copied()
        .filter(|w| {
            w.get("usedPercent")
                .and_then(Value::as_f64)
                .is_some_and(|p| p >= 100.0)
        })
        .collect();
    let reached = s
        .get("rateLimitReachedType")
        .filter(|r| crate::session::is_set(Some(r)));
    if full.is_empty() && reached.is_none() {
        return None;
    }
    let pool = if full.is_empty() { &windows } else { &full };
    let resets: Vec<f64> = pool
        .iter()
        .filter_map(|w| retry_after_seconds(w.get("resetsAt"), now_ms))
        .collect();
    Some(Exhaustion {
        reason: reached
            .cloned()
            .unwrap_or_else(|| Value::from("rate_limit_reached")),
        retry_after: resets.into_iter().reduce(f64::min),
    })
}
