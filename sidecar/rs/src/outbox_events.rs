//! outbox-events.mjs — the sidecar's rows in the fleet-tools initiative tracker.
//!
//! Every call is fail-open (the writer swallows). Never any message content — only identity,
//! model/effort and token totals.

use crate::codex::{ThreadId, Usage};
use crate::outbox::{OutboxWriter, Row};
use serde::Serialize;
use serde_json::Value;
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

/// Everything `outboxTurn(ctx, persona, sess, {model, effort, stopReason})` reads, as explicit
/// borrows.
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
    pub thread_id: Option<&'a ThreadId>,
    /// The thread's cumulative codex usage, when reported.
    pub usage_total: Option<&'a Usage>,
    pub model: &'a str,
    pub effort: Option<&'a str>,
    pub stop_reason: &'a Value,
}

#[derive(Serialize)]
struct SpawnedPayload<'a> {
    #[serde(rename = "threadId")]
    thread_id: Option<&'a ThreadId>,
}

#[derive(Serialize)]
struct TurnPayload<'a> {
    model: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    effort: Option<&'a str>,
    #[serde(rename = "threadId")]
    thread_id: Option<&'a ThreadId>,
    stop_reason: &'a Value,
}

/// `outboxUsage(u)` — the codex cumulative total in the tracker's names.
#[derive(Serialize)]
struct UsagePayload<'a> {
    model: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    effort: Option<&'a str>,
    input: u64,
    output: u64,
    total: u64,
}

#[derive(Serialize)]
struct RateLimitPayload<'a> {
    state: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    reason: Option<&'a Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    retry_after_s: Option<u64>,
}

fn row<'a, P: Serialize>(
    leg: &'a str,
    key: &'a OutboxKey,
    kind: &'static str,
    payload: P,
) -> Row<'a, P> {
    Row {
        leg,
        session_id: &key.session_id,
        agent_id: key.agent_id.as_deref(),
        kind,
        payload,
    }
}

/// The persona-wide `rate_limit` exhaustion row for one session.
pub fn rate_limit_exhausted(
    outbox: &mut OutboxWriter,
    leg: &str,
    key: &OutboxKey,
    reason: &Value,
    retry_after_s: Option<u64>,
) {
    let p = RateLimitPayload {
        state: "exhausted",
        reason: Some(reason),
        retry_after_s,
    };
    outbox.record_row(&row(leg, key, "rate_limit", p));
}

/// `outboxTurn`: one `turn.completed` per completed turn, plus `child.spawned` the first time a
/// subagent key is seen, the thread's cumulative `usage` when reported, and the `rate_limit`
/// recovery row that clears a persona-wide exhaustion.
pub fn outbox_turn(t: TurnRow<'_>) {
    let Some(okey) = t.outbox_key else { return };
    if !t.outbox_configured {
        return;
    }
    let leg = t.persona_name;
    if okey.agent_id.is_some() && !t.spawned.contains(t.sess_key) {
        t.spawned.insert(t.sess_key.to_string());
        let p = SpawnedPayload {
            thread_id: t.thread_id,
        };
        t.outbox.record_row(&row(leg, okey, "child.spawned", p));
    }
    let p = TurnPayload {
        model: t.model,
        effort: t.effort,
        thread_id: t.thread_id,
        stop_reason: t.stop_reason,
    };
    t.outbox.record_row(&row(leg, okey, "turn.completed", p));
    if let Some(u) = t.usage_total {
        let p = UsagePayload {
            model: t.model,
            effort: t.effort,
            input: u.input,
            output: u.output,
            total: u.input + u.output,
        };
        t.outbox.record_row(&row(leg, okey, "usage", p));
    }
    if *t.persona_rate_limited {
        *t.persona_rate_limited = false;
        let p = RateLimitPayload {
            state: "ok",
            reason: None,
            retry_after_s: None,
        };
        t.outbox.record_row(&row(leg, okey, "rate_limit", p));
    }
}
