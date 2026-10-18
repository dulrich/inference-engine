//! The `claude-local` native-messages leg.
//!
//! Invoked only for `claude-local`-style providers (`localLeg: "native-messages"`) on
//! Anthropic-Messages ingress: `/v1/messages` and `/v1/messages/count_tokens`
//! (`ctx.route` tells them apart). Chat-completions ingress to the same provider never calls it.

use crate::config::{EffortKwargs, MaxOutputTokens, Provider};
use crate::relay::{ByteStream, Route};
use bytes::Bytes;
use futures_util::StreamExt;
use serde_json::{json, Map, Value};
use std::sync::Arc;
use std::time::Duration;

/// Everything the leg may read about the request. Owned, so stream layers can keep it.
#[derive(Clone, Debug)]
pub struct LocalLegContext {
    /// The resolved provider; `effort_kwargs` / `max_output_tokens` are its typed config maps.
    pub provider: Arc<Provider>,
    /// The resolved bare upstream model id (already written into the body's `model`).
    pub model: String,
    pub route: Route,
    /// The client body's `stream == true`.
    pub streaming: bool,
}

/// The exact keepalive frame 0002 emits (`preStartSafeAnthropicPingFrame`).
pub const PING_FRAME: &[u8] = b"event: ping\ndata: {\"type\":\"ping\"}\n\n";
/// Byte silence after which a ping is injected (0002's interval).
pub const KEEPALIVE_INTERVAL: Duration = Duration::from_secs(15);

/// The upstream body to send, given the client body with `model` already rewritten:
/// system-message merge, effort map (0007 + 0009) and, on `/v1/messages` only, the
/// max-tokens clamp (0010). `count_tokens` gets the merge and the effort map so it counts the
/// same render the turn sends.
pub fn transform_body(mut body: Value, ctx: &LocalLegContext) -> Value {
    if !body.is_object() {
        return body;
    }
    merge_system_messages(&mut body);
    apply_effort_kwargs(&mut body, &ctx.provider.effort_kwargs, &ctx.model);
    if ctx.route == Route::Messages {
        apply_max_tokens_clamp(&mut body, &ctx.provider.max_output_tokens, &ctx.model);
    }
    body
}

/// Wraps the (telemetry-tapped) successful upstream body. The gateway always holds the client
/// response until the upstream head arrives, so upstream error statuses keep the normal 0003
/// verbatim relay; on streaming `/v1/messages` pings start once the head arrives and repeat on
/// every 15 s of silence. Everything else passes through unchanged.
pub fn wrap_stream(upstream: ByteStream, ctx: &LocalLegContext) -> ByteStream {
    if ctx.route != Route::Messages || !ctx.streaming {
        return upstream;
    }
    keepalive(upstream, KEEPALIVE_INTERVAL)
}

/// Injects `PING_FRAME` after every `interval` of byte silence, until the upstream ends.
/// Upstream bytes pass through unchanged and in order.
pub fn keepalive(upstream: ByteStream, interval: Duration) -> ByteStream {
    Box::pin(futures_util::stream::unfold(
        Some(upstream),
        move |state| async move {
            let mut inner = state?;
            let sleep = tokio::time::sleep(interval);
            tokio::pin!(sleep);
            tokio::select! {
                item = inner.next() => item.map(|i| (i, Some(inner))),
                _ = &mut sleep => Some((Ok(Bytes::from_static(PING_FRAME)), Some(inner))),
            }
        },
    ))
}

// ---- system-message merge ---------------------------------------------------------------

fn text_blocks(content: &Value) -> Vec<Value> {
    match content {
        Value::String(s) => vec![json!({"type": "text", "text": s})],
        Value::Array(a) => a.clone(),
        Value::Null => Vec::new(),
        other => vec![json!({"type": "text", "text": other.to_string()})],
    }
}

fn role_is(m: &Value, role: &str) -> bool {
    m.get("role").and_then(Value::as_str) == Some(role)
}

/// Folds every `role:"system"` entry of `messages` out of system position, keeping all text and
/// order. Rules:
/// 1. Leading system messages (before any other turn) append to the top-level `system`
///    (string + string joins with `"\n\n"`; otherwise both become text-block arrays).
/// 2. A system message whose adjacent preceding turn is `user` appends its blocks to that turn
///    (string content becomes a text-block array). Consecutive system messages therefore all
///    land, in order, on the same turn.
/// 3. A system message after a non-user turn (no adjacent preceding user turn) is held and
///    prepended to the next `user` turn; if the next turn is not `user` (or there is none),
///    the held blocks become a new `user` turn in that position.
///
/// A body with no system message inside `messages` is untouched.
pub fn merge_system_messages(body: &mut Value) {
    let Some(msgs) = body.get("messages").and_then(Value::as_array) else {
        return;
    };
    if !msgs.iter().any(|m| role_is(m, "system")) {
        return;
    }
    let msgs = msgs.clone();
    let mut out: Vec<Value> = Vec::with_capacity(msgs.len());
    let mut top: Vec<Value> = Vec::new();
    let mut held: Vec<Value> = Vec::new();
    for mut m in msgs {
        let content = m.get("content").cloned().unwrap_or(Value::Null);
        if role_is(&m, "system") {
            match out.last_mut() {
                None => top.push(content),
                Some(last) if held.is_empty() && role_is(last, "user") => {
                    let mut blocks = text_blocks(&last["content"]);
                    blocks.extend(text_blocks(&content));
                    last["content"] = Value::Array(blocks);
                }
                Some(_) => held.extend(text_blocks(&content)),
            }
            continue;
        }
        if !held.is_empty() {
            let mut blocks = std::mem::take(&mut held);
            if role_is(&m, "user") {
                blocks.extend(text_blocks(&content));
                m["content"] = Value::Array(blocks);
            } else {
                out.push(json!({"role": "user", "content": blocks}));
            }
        }
        out.push(m);
    }
    if !held.is_empty() {
        out.push(json!({"role": "user", "content": held}));
    }
    for content in top {
        let sys = body.get("system").cloned().unwrap_or(Value::Null);
        let next = match (&sys, &content) {
            (Value::Null, _) => content,
            (Value::String(a), Value::String(b)) => Value::String(format!("{a}\n\n{b}")),
            _ => {
                let mut blocks = text_blocks(&sys);
                blocks.extend(text_blocks(&content));
                Value::Array(blocks)
            }
        };
        body["system"] = next;
    }
    body["messages"] = Value::Array(out);
}

// ---- effort map (0007 + 0009) -----------------------------------------------------------

fn norm(v: Option<&Value>) -> Option<String> {
    v.and_then(Value::as_str).map(|s| s.trim().to_lowercase())
}

/// 0007/0009 level derivation: `thinking.type == disabled`, else `output_config.effort`
/// (each of the five levels onto itself), else an `enabled` numeric `budget_tokens`
/// (`< 2048` low, `< 8192` medium, else high), else none.
pub fn derive_effort_level(body: &Value) -> Option<&'static str> {
    let thinking = body.get("thinking").filter(|t| t.is_object());
    let ttype = norm(thinking.and_then(|t| t.get("type")));
    if ttype.as_deref() == Some("disabled") {
        return Some("disabled");
    }
    let effort = norm(
        body.get("output_config")
            .filter(|o| o.is_object())
            .and_then(|o| o.get("effort")),
    );
    if let Some(e) = effort {
        if let Some(l) = ["low", "medium", "high", "xhigh", "max"]
            .into_iter()
            .find(|l| *l == e)
        {
            return Some(l);
        }
    }
    if ttype.as_deref() == Some("enabled") {
        if let Some(b) = thinking
            .and_then(|t| t.get("budget_tokens"))
            .and_then(Value::as_f64)
        {
            return Some(if b < 2048.0 {
                "low"
            } else if b < 8192.0 {
                "medium"
            } else {
                "high"
            });
        }
    }
    None
}

/// Deep merge with keys already on `existing` winning.
fn merge_kwargs(configured: &Map<String, Value>, existing: Option<&Value>) -> Value {
    let Some(Value::Object(existing)) = existing else {
        return Value::Object(configured.clone());
    };
    let mut merged = configured.clone();
    for (k, v) in existing {
        let next = match (merged.get(k), v) {
            (Some(Value::Object(c)), Value::Object(_)) => merge_kwargs(c, Some(v)),
            _ => v.clone(),
        };
        merged.insert(k.clone(), next);
    }
    Value::Object(merged)
}

/// Merges the model's row for the derived level into `chat_template_kwargs`: the exact row,
/// else (`xhigh`/`max` only) the `high` row, else a no-op. Never emits an unmapped level.
pub fn apply_effort_kwargs(body: &mut Value, effort_kwargs: &EffortKwargs, model: &str) {
    let Some(levels) = effort_kwargs.get(model) else {
        return;
    };
    let Some(level) = derive_effort_level(body) else {
        return;
    };
    let row = levels.get(level).or_else(|| {
        if matches!(level, "xhigh" | "max") {
            levels.get("high")
        } else {
            None
        }
    });
    let Some(row) = row else {
        return;
    };
    let merged = merge_kwargs(row, body.get("chat_template_kwargs"));
    body["chat_template_kwargs"] = merged;
}

// ---- max-tokens clamp (0010) ------------------------------------------------------------

/// Downward only; absent/null → the cap; present but not a finite number → untouched. The cap
/// map is validated positive at load.
pub fn apply_max_tokens_clamp(body: &mut Value, max_output_tokens: &MaxOutputTokens, model: &str) {
    let Some(&cap) = max_output_tokens.get(model) else {
        return;
    };
    let clamp = match body.get("max_tokens") {
        None | Some(Value::Null) => true,
        Some(v) => v.as_f64().is_some_and(|n| n.is_finite() && n > cap as f64),
    };
    if clamp {
        body["max_tokens"] = Value::from(cap);
    }
}
