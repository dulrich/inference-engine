//! errors.mjs — the sidecar's error taxonomy and the upstream -> Anthropic mapping.
//!
//! One owner for "what does the client see when something fails": the `SidecarError` value,
//! the CodexErrorInfo/TurnError translation, and the rate-limit reading that turns an account
//! snapshot into a 429 with a retry-after. No HTTP, session or child process here.

use crate::js;
use serde_json::{json, Map, Value};
use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub struct SidecarError {
    pub status: u16,
    pub anthropic_type: String,
    pub message: String,
    /// `extra` in Node; the only key read today is `retryAfter` (sinks: `retry-after` header).
    pub extra: Option<Box<Map<String, Value>>>,
}

impl SidecarError {
    pub fn new(status: u16, anthropic_type: &str, message: impl Into<String>) -> Self {
        Self {
            status,
            anthropic_type: anthropic_type.into(),
            message: message.into(),
            extra: None,
        }
    }

    pub fn with_extra(mut self, extra: Map<String, Value>) -> Self {
        self.extra = Some(Box::new(extra));
        self
    }

    /// `{type:"error", error:{type, message}}` — the Anthropic error body.
    pub fn body(&self) -> Value {
        json!({ "type": "error", "error": { "type": self.anthropic_type, "message": self.message } })
    }
}

impl fmt::Display for SidecarError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for SidecarError {}

/// `asSidecarError`: any non-SidecarError failure becomes a 502 `api_error` carrying its
/// message (`String(e?.message || e || "sidecar failure")`).
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
            if js::is_line_terminator(c) || max.is_some_and(|m| gap >= m) {
                return false;
            }
        }
        false
    })
}

fn invalid_api_key(s: &str) -> bool {
    s.match_indices("invalid").any(|(i, _)| {
        let line = s[i..].split(js::is_line_terminator).next().unwrap_or("");
        line.match_indices("api")
            .any(|(j, _)| has_gap(&line[j..], "api", "key", Some(1)))
    })
}

/// ASCII-only case folding: JS `/i` without `u` never folds non-ASCII onto ASCII.
fn ascii_lower(s: &str) -> String {
    s.chars().map(|c| c.to_ascii_lowercase()).collect()
}

/// `mapUpstreamError(err, ctxLabel = "app-server")`: an app-server JSON-RPC error / TurnError
/// onto an Anthropic error shape.
pub fn map_upstream_error(err: &Value, ctx_label: &str) -> SidecarError {
    let obj = err.as_object();
    let text = match err {
        Value::String(s) => s.clone(),
        Value::Null => "{}".into(),
        other => js::stringify(other),
    };
    let get = |k: &str| obj.and_then(|o| js::present(o.get(k)));
    let msg = get("message")
        .or_else(|| js::present(js::prop(get("error"), "message")))
        .map_or_else(|| text.clone(), js::js_string);
    let info = get("codexErrorInfo")
        .or_else(|| js::present(js::prop(get("error"), "codexErrorInfo")))
        .or_else(|| js::present(js::prop(get("data"), "codexErrorInfo")));
    let info_key: Option<String> = match info {
        Some(Value::String(s)) => Some(s.clone()),
        Some(v @ (Value::Object(_) | Value::Array(_))) => {
            js::entries(v).into_iter().next().map(|(k, _)| k)
        }
        _ => None,
    };
    let full = format!("{ctx_label}: {msg}");
    if let Some(k) = &info_key {
        if let Some((_, status, ty)) = CODEX_ERROR_MAP.iter().find(|(n, _, _)| n == k) {
            return SidecarError::new(*status, ty, full);
        }
    }
    // object CodexErrorInfo variants forward the upstream HTTP status verbatim
    let http_status = match (&info_key, info) {
        (Some(k), Some(v @ (Value::Object(_) | Value::Array(_)))) => js::entries(v)
            .into_iter()
            .find(|(n, _)| n == k)
            .and_then(|(_, x)| js::prop(Some(x), "httpStatusCode"))
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

/// `retryAfterSeconds(resetsAt, now)`: RateLimitWindow.resetsAt is an absolute int64; tolerate
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

/// `rateLimitExhaustion(snapshot)`: exhausted when a window is at/over 100 % or a reached-type
/// is set.
pub fn rate_limit_exhaustion(snapshot: Option<&Value>, now_ms: f64) -> Option<Exhaustion> {
    let s = snapshot.filter(|v| v.is_object() || v.is_array())?;
    let windows: Vec<&Value> = ["primary", "secondary"]
        .iter()
        .filter_map(|k| js::prop(Some(s), k))
        .filter(|w| w.is_object() || w.is_array())
        .collect();
    let full: Vec<&Value> = windows
        .iter()
        .copied()
        .filter(|w| {
            js::prop(Some(w), "usedPercent")
                .and_then(Value::as_f64)
                .is_some_and(|p| p >= 100.0)
        })
        .collect();
    let reached = js::prop(Some(s), "rateLimitReachedType");
    if full.is_empty() && !js::truthy(reached) {
        return None;
    }
    let pool = if full.is_empty() { &windows } else { &full };
    let resets: Vec<f64> = pool
        .iter()
        .filter_map(|w| retry_after_seconds(js::prop(Some(w), "resetsAt"), now_ms))
        .collect();
    Some(Exhaustion {
        reason: if js::truthy(reached) {
            reached.cloned().unwrap_or(Value::Null)
        } else {
            Value::from("rate_limit_reached")
        },
        retry_after: resets.into_iter().reduce(f64::min),
    })
}
