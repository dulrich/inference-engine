//! Outcome emission (phase 4 of POST /<persona>/v1/messages): the client-facing blocks and
//! finish, the stored last outcome, and the one `http`/`http-error` row per request with its
//! outbox rows.

use crate::codex::{ToolCall, Usage};
use crate::errors::SidecarError;
use crate::logging::redact_headers;
use crate::messages::Rc;
use crate::outbox_events::{outbox_turn, TurnRow};
use crate::protocol::{clamp_usage, usage_out};
use crate::request::{short_digest, Identity};
use crate::session::{settle_joiner, JoinOutcome, LastOutcome, Outcome};
use crate::sidecar::World;
use crate::sinks;
use serde_json::{json, Map, Value};

fn effort_val(ident: &Identity) -> Option<Value> {
    ident.eff.effort.as_deref().map(Value::from)
}

/// Which kind of request an `http`/`http-error` row is for.
pub(crate) enum Role {
    /// The request that drove the turn; `deduped` tool results are logged when non-zero.
    Owner { deduped: usize },
    /// A request that joined another's turn; carries the owner's `reqId`.
    Joiner { owner_req: Value },
    /// A request answered from the session's stored last outcome.
    Replay,
}

impl Role {
    /// The role's own trailing fields on a row.
    fn put_fields(&self, row: &mut Map<String, Value>) {
        match self {
            Role::Owner { deduped } if *deduped > 0 => {
                row.insert("dedupedToolResults".into(), Value::from(*deduped));
            }
            Role::Joiner { owner_req } => {
                row.insert("joinedTo".into(), owner_req.clone());
            }
            Role::Owner { .. } | Role::Replay => {}
        }
    }

    /// Joiners share the owner's turn, so only owner and replay rows write outbox rows.
    fn writes_outbox(&self) -> bool {
        !matches!(self, Role::Joiner { .. })
    }
}

/// The per-request inputs of one `http` row.
pub(crate) struct HttpRow<'a> {
    pub stop_reason: &'a Value,
    pub tool_calls: &'a Value,
    pub usage: &'a Usage,
    pub usage_raw: Option<&'a Usage>,
    pub role: Role,
}

/// One `http` row per request (owner, joiner, replay), and the outbox rows for a turn.
pub(crate) fn log_http(w: &mut World, rc: &Rc, h: HttpRow<'_>) {
    let HttpRow {
        stop_reason,
        tool_calls,
        usage,
        usage_raw,
        role,
    } = h;
    let (tid, total) = w
        .sessions
        .get(&rc.sid)
        .map(|s| (s.thread_id.clone(), s.usage_total.clone()))
        .unwrap_or_default();
    let n = &rc.norm;
    let mut row = Map::new();
    let mut put = |k: &str, v: Value| {
        row.insert(k.into(), v);
    };
    put("persona", Value::from(rc.persona.as_str()));
    put("path", Value::from(format!("/{}/v1/messages", rc.persona)));
    put("key", Value::from(rc.ident.key.as_str()));
    put("reqId", Value::from(rc.req_id.as_str()));
    put("scope", Value::from(rc.ident.scope));
    put("mode", Value::from(rc.mode));
    put("stream", Value::from(n.stream));
    put("model", Value::from(rc.ident.parsed.model.as_str()));
    if let Some(e) = effort_val(&rc.ident) {
        put("effort", e);
    }
    put("effortSource", Value::from(rc.ident.eff.effort_source));
    put("threadId", tid.as_ref().map_or(Value::Null, Value::from));
    put("stop_reason", stop_reason.clone());
    put("nMessages", Value::from(n.msgs.len()));
    put("nSystemRole", Value::from(n.other_msgs.len()));
    put("nTools", Value::from(n.req.tool_list().len()));
    put("toolCalls", tool_calls.clone());
    put("usage", usage_out(Some(usage)));
    put("usageCodex", usage_raw.unwrap_or(usage).to_value());
    // the cumulative figure exactly as codex sent it (every field, and its presence)
    put(
        "usageTotalCodex",
        total.as_ref().map_or(Value::Null, |u| u.raw.clone()),
    );
    put("headers", Value::Object(redact_headers(&rc.headers)));
    role.put_fields(&mut row);
    w.log("http", Value::Object(row));
    if !role.writes_outbox() {
        return;
    }
    let World {
        outbox,
        outbox_spawned,
        personas,
        sessions,
        cfg,
        ..
    } = w;
    let Some(s) = sessions.get(&rc.sid) else {
        return;
    };
    outbox_turn(TurnRow {
        outbox,
        outbox_configured: !cfg.outbox_path.is_empty(),
        spawned: outbox_spawned,
        persona_name: &rc.persona,
        persona_rate_limited: &mut personas[rc.pi].rate_limited,
        sess_key: &s.key,
        outbox_key: s.outbox_key.as_ref(),
        thread_id: tid.as_ref(),
        usage_total: total.as_ref().map(|u| &u.usage),
        model: &rc.ident.parsed.model,
        effort: rc.ident.eff.effort.as_deref(),
        stop_reason,
    });
}

pub(crate) fn http_error_row(rc: &Rc, e: &SidecarError, role: &Role) -> Value {
    let mut row = json!({
        "persona": rc.persona, "key": rc.ident.key, "reqId": rc.req_id, "mode": rc.mode.as_str(),
        "status": e.status, "type": e.anthropic_type, "message": e.message,
        "headers": redact_headers(&rc.headers),
    });
    if let Some(o) = row.as_object_mut() {
        role.put_fields(o);
    }
    if let (Some(u), Some(o)) = (&e.upstream_message, row.as_object_mut()) {
        o.insert("upstreamMessage".into(), Value::from(u.as_str()));
    }
    row
}

// --------------------------------------------------------- REPLAY outcome ----

pub(crate) fn emit_replay(w: &mut World, rc: &Rc, lo: &LastOutcome) {
    w.log(
        "replayed-outcome",
        json!({ "persona": rc.persona, "key": rc.ident.key, "reqId": rc.req_id, "ownerReqId": lo.req_id, "reqDigest": short_digest(&rc.token.digest) }),
    );
    let lag = w.sessions[&rc.sid].lag_usage();
    sinks::start(w, rc.sink, &lag);
    for b in &lo.blocks {
        if b.get("type") == Some(&Value::from("text")) {
            let t = b.get("text").and_then(Value::as_str).unwrap_or("");
            sinks::text_delta(w, rc.sink, t);
        } else {
            sinks::tool_use(w, rc.sink, b);
        }
    }
    sinks::finish(w, rc.sink, &lo.stop_reason, &lo.usage);
    log_http(
        w,
        rc,
        HttpRow {
            stop_reason: &lo.stop_reason,
            tool_calls: &lo.tool_calls,
            usage: &lo.usage,
            usage_raw: None,
            role: Role::Replay,
        },
    );
}

fn tool_block(call: &ToolCall) -> Value {
    let p = &call.params;
    let mut b = Map::new();
    b.insert("type".into(), Value::from("tool_use"));
    if let Some(id) = &p.call_id {
        b.insert("id".into(), Value::from(id));
    }
    if let Some(n) = &p.tool {
        b.insert("name".into(), Value::from(n.as_str()));
    }
    b.insert(
        "input".into(),
        p.arguments.clone().unwrap_or_else(|| json!({})),
    );
    Value::Object(b)
}

pub(crate) fn emit_outcome(
    w: &mut World,
    rc: &Rc,
    out: Outcome,
    audience: &[u64],
    joiners: &[u64],
    deduped: usize,
) {
    let batch = match &out {
        Outcome::ToolUse(b) => b.clone(),
        _ => Vec::new(),
    };
    let is_tool = matches!(out, Outcome::ToolUse(_));
    let stop = Value::from(if is_tool { "tool_use" } else { "end_turn" });
    let tool_calls = Value::Array(
        batch
            .iter()
            .map(|c| c.params.tool.as_deref().map_or(Value::Null, Value::from))
            .collect(),
    );
    let blocks: Vec<Value> = batch.iter().map(tool_block).collect();
    if is_tool {
        for (call, block) in batch.iter().zip(&blocks) {
            if let (Some(id), Some(s)) = (&call.params.call_id, w.sessions.get_mut(&rc.sid)) {
                s.delivered.insert(id.0.clone());
            }
            for sk in audience {
                sinks::tool_use(w, *sk, block);
            }
        }
        if batch.len() > 1 {
            let tid = w.sessions[&rc.sid]
                .thread_id
                .as_ref()
                .map_or(Value::Null, Value::from);
            w.log(
                "parallel-tool-batch",
                json!({ "persona": rc.persona, "threadId": tid, "n": batch.len() }),
            );
        }
    }
    let Some(s) = w.sessions.get_mut(&rc.sid) else {
        return;
    };
    s.turn_open = is_tool;
    let usage_raw = s.reconcile();
    let (window, src) = match s.context_window {
        Some(v) => (v, "app-server"),
        None => (w.cfg.context_window_tokens, "env"),
    };
    let tid = s.thread_id.as_ref().map_or(Value::Null, Value::from);
    let clamped = clamp_usage(&usage_raw, window);
    let usage = clamped.unwrap_or(usage_raw);
    if let Some(c) = &clamped {
        w.log(
            "usage-clamped",
            json!({ "persona": rc.persona, "key": rc.ident.key, "threadId": tid, "window": window,
                    "windowSource": src, "raw": usage_raw.to_value(), "clamped": usage_out(Some(c)) }),
        );
    }
    for sk in audience {
        sinks::finish(w, *sk, &stop, &usage);
    }
    if let Some(s) = w.sessions.get_mut(&rc.sid) {
        let emitted = s.turn_text.concat();
        let mut lb = Vec::new();
        if !emitted.is_empty() {
            lb.push(json!({ "type": "text", "text": emitted }));
        }
        lb.extend(blocks);
        s.last_outcome = Some(LastOutcome {
            req_id: rc.req_id.clone(),
            digest: rc.token.digest.clone(),
            stop_reason: stop.clone(),
            tool_calls: tool_calls.clone(),
            usage,
            blocks: lb,
        });
    }
    log_http(
        w,
        rc,
        HttpRow {
            stop_reason: &stop,
            tool_calls: &tool_calls,
            usage: &usage,
            usage_raw: Some(&usage_raw),
            role: Role::Owner { deduped },
        },
    );
    for j in joiners {
        settle_joiner(
            w,
            *j,
            JoinOutcome::Done {
                stop_reason: stop.clone(),
                tool_calls: tool_calls.clone(),
                usage,
            },
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn role_fields_and_outbox_decision() {
        let fields = |r: &Role| {
            let mut m = Map::new();
            r.put_fields(&mut m);
            Value::Object(m)
        };
        let joiner = Role::Joiner {
            owner_req: Value::from("req-1"),
        };
        assert_eq!(fields(&Role::Owner { deduped: 0 }), json!({}));
        assert_eq!(
            fields(&Role::Owner { deduped: 2 }),
            json!({ "dedupedToolResults": 2 })
        );
        assert_eq!(fields(&joiner), json!({ "joinedTo": "req-1" }));
        assert_eq!(fields(&Role::Replay), json!({}));
        assert!(Role::Owner { deduped: 0 }.writes_outbox());
        assert!(Role::Replay.writes_outbox());
        assert!(!joiner.writes_outbox());
    }
}
