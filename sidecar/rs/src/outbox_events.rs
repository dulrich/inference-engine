//! outbox-events.mjs — the sidecar's rows in the fleet-tools initiative tracker.
//!
//! Every call is fail-open (the writer swallows). Never any message content — only identity,
//! model/effort and token totals.

use crate::js;
use crate::outbox::OutboxWriter;
use serde_json::{Map, Value};
use std::collections::HashSet;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutboxKey {
    pub session_id: String,
    pub agent_id: Option<String>,
}

/// `outboxKeyOf(key, weak)`: a parent's key is the bare session id, a subagent's is
/// `<sid>:<suffix>`; a weak key is not attributable and writes nothing.
pub fn outbox_key_of(key: &str, weak: bool) -> Option<OutboxKey> {
    if weak || key.is_empty() {
        return None;
    }
    Some(match key.split_once(':') {
        None => OutboxKey {
            session_id: key.into(),
            agent_id: None,
        },
        Some((s, a)) => OutboxKey {
            session_id: s.into(),
            agent_id: Some(a.into()),
        },
    })
}

/// JS `a + b` for the two token counts (numbers add; a string operand concatenates).
fn js_add(a: &Value, b: &Value) -> Value {
    if a.is_string()
        || b.is_string()
        || a.is_object()
        || a.is_array()
        || b.is_object()
        || b.is_array()
    {
        return Value::from(format!("{}{}", js::js_string(a), js::js_string(b)));
    }
    js::num(js::to_number(Some(a)) + js::to_number(Some(b)))
}

/// `outboxUsage(u)` — the codex cumulative total in the tracker's names.
fn outbox_usage(u: Option<&Value>, into: &mut Map<String, Value>) {
    let input = js::present(js::prop(u, "inputTokens"))
        .cloned()
        .unwrap_or(Value::from(0));
    let output = js::present(js::prop(u, "outputTokens"))
        .cloned()
        .unwrap_or(Value::from(0));
    let total = js_add(&input, &output);
    into.insert("input".into(), input);
    into.insert("output".into(), output);
    into.insert("total".into(), total);
}

/// Everything `outboxTurn(ctx, persona, sess, {model, effort, stopReason})` reads, as explicit
/// borrows (WP-3 owns the ctx/persona/session types). `None` for model/effort/stop_reason means
/// JS `undefined` (key omitted); `Some(Value::Null)` is a written `null`.
pub struct TurnRow<'a> {
    pub outbox: &'a mut OutboxWriter,
    /// `ctx.config.outboxPath` non-empty.
    pub outbox_configured: bool,
    /// `ctx.outboxSpawned` (session keys whose `child.spawned` row is written).
    pub spawned: &'a mut HashSet<String>,
    pub persona_name: &'a str,
    /// `persona.rateLimited`; cleared when the recovery row is written.
    pub persona_rate_limited: &'a mut bool,
    pub sess_key: &'a str,
    pub outbox_key: Option<&'a OutboxKey>,
    pub thread_id: Option<Value>,
    pub usage_total: Option<&'a Value>,
    pub model: Option<Value>,
    pub effort: Option<Value>,
    pub stop_reason: Option<Value>,
}

fn put(m: &mut Map<String, Value>, k: &str, v: &Option<Value>) {
    if let Some(v) = v {
        m.insert(k.into(), v.clone());
    }
}

/// `outboxTurn`: one `turn.completed` per completed turn, plus `child.spawned` the first time a
/// subagent key is seen, the thread's cumulative `usage` when reported, and the `rate_limit`
/// recovery row that clears a persona-wide exhaustion.
pub fn outbox_turn(t: TurnRow<'_>) {
    let Some(okey) = t.outbox_key else { return };
    if !t.outbox_configured {
        return;
    }
    let base = |kind: &str, payload: Map<String, Value>| {
        let mut m = Map::new();
        m.insert("harness".into(), Value::from("codex-sidecar"));
        m.insert("leg".into(), Value::from(t.persona_name));
        m.insert("session_id".into(), Value::from(okey.session_id.as_str()));
        m.insert(
            "agent_id".into(),
            okey.agent_id.as_deref().map_or(Value::Null, Value::from),
        );
        m.insert("agent_type".into(), Value::Null);
        m.insert("kind".into(), Value::from(kind));
        m.insert("payload".into(), Value::Object(payload));
        Value::Object(m)
    };
    if okey.agent_id.is_some() && !t.spawned.contains(t.sess_key) {
        t.spawned.insert(t.sess_key.to_string());
        let mut p = Map::new();
        put(&mut p, "threadId", &t.thread_id);
        t.outbox.record(&base("child.spawned", p));
    }
    let mut p = Map::new();
    put(&mut p, "model", &t.model);
    put(&mut p, "effort", &t.effort);
    put(&mut p, "threadId", &t.thread_id);
    put(&mut p, "stop_reason", &t.stop_reason);
    t.outbox.record(&base("turn.completed", p));
    if js::truthy(t.usage_total) {
        let mut p = Map::new();
        put(&mut p, "model", &t.model);
        put(&mut p, "effort", &t.effort);
        outbox_usage(t.usage_total, &mut p);
        t.outbox.record(&base("usage", p));
    }
    if *t.persona_rate_limited {
        *t.persona_rate_limited = false;
        let mut p = Map::new();
        p.insert("state".into(), Value::from("ok"));
        t.outbox.record(&base("rate_limit", p));
    }
}
