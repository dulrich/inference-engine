//! `messages.mjs` — POST /<persona>/v1/messages as the explicit four-phase sequence:
//! normalization, session disposition (replay | join | drive), turn execution, outcome
//! emission. Every exit writes one `http` (or `http-error`) row and settles every attached
//! connection.

use crate::errors::SidecarError;
use crate::js;
use crate::logging::redact_headers;
use crate::outbox_events::{outbox_key_of, outbox_turn, TurnRow};
use crate::persona;
use crate::protocol::{
    clamp_usage, has_image_block, is_title_request, title_envelope, usage_out, Scope,
};
use crate::request::{
    disposition_of, message_digests, msg_diff, normalize_request, request_token, resolve_identity,
    short_digest, validate, Disposition, Identity, Mode, Normalized, Token,
};
use crate::session::{
    self, attach, detach, id_key, joiners_of, remove_joiner, settle_joiner, JoinOutcome, Joiner,
    LastOutcome, Outcome, OwnerTok, Session,
};
use crate::sidecar::{err, lock, random_hex, Shared, World};
use crate::sinks::{self, message_skeleton, wire_model_id, with_keys, CloseHook, Reply, Sink};
use crate::turn::{deliver_tool_results, start_thread, start_turn, still_mine};
use axum::http::HeaderMap;
use serde_json::{json, Map, Value};
use std::collections::HashSet;
use tokio::sync::oneshot;

pub fn superseded() -> SidecarError {
    err(
        409,
        "api_error",
        "superseded by a newer request on this session",
    )
}

/// The request context (`rc`).
pub struct Rc {
    pub pi: usize,
    pub persona: String,
    pub headers: HeaderMap,
    pub norm: Normalized,
    pub ident: Identity,
    pub sid: u64,
    pub mode: Mode,
    pub req_id: String,
    pub token: Token,
    pub token_id: u64,
    pub sink: u64,
}

enum Plan {
    Done,
    Join {
        owner: u64,
        owner_req: Value,
    },
    Drive {
        supersedes: Option<(u64, Box<Token>)>,
    },
}

/// Phase 1 resolved: what `run` needs to drive the request.
pub struct Admitted {
    pi: usize,
    headers: HeaderMap,
    norm: Normalized,
    ident: Identity,
    digests: Vec<String>,
    init: Option<oneshot::Receiver<persona::InitResult>>,
}

/// What phase 1 decided: a local answer (title, fork) or a request to run.
pub enum Admit {
    Local(Reply),
    Run(Box<Admitted>),
}

/// Phase 1: validation and normalization (nothing is touched before the shape check),
/// the local answers, and the persona boot. One critical section, no await.
pub fn admit(
    me: &Shared,
    pi: usize,
    body: Value,
    headers: HeaderMap,
) -> Result<Admit, SidecarError> {
    validate(&body)?;
    let norm = normalize_request(body, Some(headers.clone()));
    let mut w = lock(me);
    let w = &mut *w;
    let name = w.personas[pi].name.clone();
    if is_title_request(&norm.body, &norm.system_text, &norm.msgs) {
        w.log(
            "title-local",
            json!({ "persona": name, "stream": norm.stream, "envelope": title_envelope(&norm.msgs) }),
        );
        return Ok(Admit::Local(local_text(&name, &norm.body, "Session")));
    }
    if has_image_block(&norm.body) {
        return Err(err(
            400,
            "invalid_request_error",
            "images unsupported by the codex app-server leg",
        ));
    }
    let ident = resolve_identity(&norm)?;
    if ident.weak {
        w.log(
            "weak-session-key",
            json!({ "persona": name, "key": ident.key, "reason": "metadata.user_id carried no session_id" }),
        );
    }
    let digests = message_digests(&norm.msgs);
    if let Some(fork) = find_fork_target(w, pi, &norm, &ident, &digests) {
        let (fk, seen) = w
            .sessions
            .get(&fork)
            .map(|s| (s.key.clone(), s.hist.len()))
            .unwrap_or_default();
        w.log(
            "fork-local",
            json!({ "persona": name, "key": ident.key, "forkOf": fk, "n": norm.msgs.len(), "seen": seen }),
        );
        return Ok(Admit::Local(local_text(&name, &norm.body, FORK_TEXT)));
    }
    let init = persona::ensure(w, pi)?;
    Ok(Admit::Run(Box::new(Admitted {
        pi,
        headers,
        norm,
        ident,
        digests,
        init,
    })))
}

/// Phases 2-4. `sink` is the id the HTTP layer's close guard watches; `reply` moves into
/// the sink once it exists, so an `Err` here means the head has not been sent.
pub async fn run(
    me: &Shared,
    a: Admitted,
    sink: u64,
    reply: &mut Option<oneshot::Sender<Reply>>,
) -> Result<(), SidecarError> {
    let Admitted {
        pi,
        headers,
        norm,
        ident,
        digests,
        init,
    } = a;
    if let Some(rx) = init {
        rx.await
            .unwrap_or_else(|_| Err(err(502, "api_error", "persona boot dropped")))?;
    }

    // 2. session disposition
    let (rc, plan) = {
        let mut w = lock(me);
        let w = &mut *w;
        let (sid, mode) = dispose_session(w, pi, &norm, &ident, digests);
        let req_id = random_hex(4);
        let name = w.personas[pi].name.clone();
        w.log(
            "http-start",
            json!({ "persona": name, "key": ident.key, "reqId": req_id, "mode": mode.as_str(), "stream": norm.stream, "nMessages": norm.msgs.len() }),
        );
        let wire = wire_model_id(&name, js::prop(Some(&norm.body), "model"));
        let Some(tx) = reply.take() else {
            return Ok(());
        };
        let sk = if norm.stream {
            Sink::sse(tx, &wire, w.cfg.ping_ms)
        } else {
            Sink::buffer(tx, &wire)
        };
        w.sinks.insert(sink, sk);
        let token = request_token(Value::from(req_id.as_str()), mode, &norm, &ident);
        let token_id = w.id();
        let rc = Rc {
            pi,
            persona: name,
            headers,
            norm,
            ident,
            sid,
            mode,
            req_id,
            token,
            token_id,
            sink,
        };
        let sess = &w.sessions[&sid];
        let plan = match disposition_of(sess, &rc.token) {
            Disposition::Replay { outcome } => {
                let lo = LastOutcome {
                    req_id: outcome.req_id.clone(),
                    digest: outcome.digest.clone(),
                    stop_reason: outcome.stop_reason.clone(),
                    tool_calls: outcome.tool_calls.clone(),
                    usage: outcome.usage.clone(),
                    blocks: outcome.blocks.clone(),
                };
                emit_replay(w, &rc, &lo);
                Plan::Done
            }
            Disposition::Join { owner } => Plan::Join {
                owner: owner.id,
                owner_req: owner.token.req_id.clone(),
            },
            Disposition::Drive { supersedes } => Plan::Drive {
                supersedes: supersedes.map(|o| (o.id, Box::new(o.token.clone()))),
            },
        };
        (rc, plan)
    };
    // 3 + 4. one of exactly three paths; only `drive` runs a turn.
    match plan {
        Plan::Done => {
            finish_request(me, &rc);
            Ok(())
        }
        Plan::Join { owner, owner_req } => {
            run_as_joiner(me, &rc, owner, owner_req).await;
            Ok(())
        }
        Plan::Drive { supersedes } => {
            run_as_owner(me, &rc, supersedes).await;
            Ok(())
        }
    }
}

fn finish_request(me: &Shared, rc: &Rc) {
    let mut w = lock(me);
    w.sinks.remove(&rc.sink);
    w.gc_sessions();
}

// --------------------------------------------------------------- phase 1 ----

const FORK_TEXT: &str = "(side query answered locally by codex-sidecar; no model call was made)";

fn local_text(persona: &str, body: &Value, text: &str) -> Reply {
    let usage = json!({ "input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0 });
    let model = wire_model_id(persona, js::prop(Some(body), "model"));
    if js::truthy(js::prop(Some(body), "stream")) {
        let msg = with_keys(&message_skeleton(&model), vec![("usage", usage.clone())]);
        let frames = [
            (
                "message_start",
                json!({ "type": "message_start", "message": msg }),
            ),
            (
                "content_block_start",
                json!({ "type": "content_block_start", "index": 0, "content_block": { "type": "text", "text": "" } }),
            ),
            (
                "content_block_delta",
                json!({ "type": "content_block_delta", "index": 0, "delta": { "type": "text_delta", "text": text } }),
            ),
            (
                "content_block_stop",
                json!({ "type": "content_block_stop", "index": 0 }),
            ),
            (
                "message_delta",
                json!({ "type": "message_delta", "delta": { "stop_reason": "end_turn", "stop_sequence": null }, "usage": usage }),
            ),
            ("message_stop", json!({ "type": "message_stop" })),
        ];
        let sse: String = frames
            .iter()
            .map(|(t, o)| format!("event: {t}\ndata: {}\n\n", js::stringify(o)))
            .collect();
        return Reply::sse(sse);
    }
    let body = with_keys(
        &message_skeleton(&model),
        vec![
            ("content", json!([{ "type": "text", "text": text }])),
            ("stop_reason", Value::from("end_turn")),
            ("usage", usage),
        ],
    );
    Reply::json(200, &body, Vec::new())
}

// --------------------------------------------------------------- phase 2 ----

enum Cls {
    Fork,
    Continues,
    Diverged {
        at: Option<usize>,
        step: Option<i64>,
    },
}

/// `classifyAgainst(sess, digests, norm, ident)`.
fn classify_against(
    hist: &[String],
    digests: &[String],
    norm: &Normalized,
    ident: &Identity,
) -> Cls {
    let m = hist.len();
    let n = digests.len();
    let first_diff = |upto: usize| (0..upto).find(|&i| digests.get(i) != hist.get(i));
    if n < m {
        let at = first_diff(n);
        if at.is_none() && ident.scope == Scope::Subagent && !norm.stream {
            return Cls::Fork;
        }
        return match at {
            None => Cls::Diverged {
                at: None,
                step: Some(n as i64 - m as i64),
            },
            Some(a) => Cls::Diverged {
                at: Some(a),
                step: None,
            },
        };
    }
    if let Some(a) = first_diff(m.saturating_sub(1)) {
        return Cls::Diverged {
            at: Some(a),
            step: None,
        };
    }
    if n == m {
        return Cls::Continues;
    }
    if (n == m + 2 || n == m + 1) && (m == 0 || digests[m - 1] == hist[m - 1]) {
        return Cls::Continues;
    }
    if m > 0 && digests[m - 1] != hist[m - 1] {
        return Cls::Diverged {
            at: Some(m - 1),
            step: None,
        };
    }
    Cls::Diverged {
        at: None,
        step: Some(n as i64 - m as i64),
    }
}

fn classify(
    w: &World,
    sid: u64,
    digests: &[String],
    norm: &Normalized,
    ident: &Identity,
) -> Option<Cls> {
    w.sessions
        .get(&sid)
        .map(|s| classify_against(&s.hist, digests, norm, ident))
}

fn suffix_of(key: &str) -> Option<&str> {
    key.find(':').map(|i| &key[i..])
}

fn rekey_candidates(w: &World, pi: usize, ident: &Identity) -> Vec<u64> {
    if ident.scope != Scope::Subagent || ident.weak {
        return Vec::new();
    }
    let Some(suffix) = suffix_of(&ident.key) else {
        return Vec::new();
    };
    w.personas[pi]
        .sessions
        .iter()
        .map(|(_, s)| *s)
        .filter(|sid| {
            w.sessions.get(sid).is_some_and(|s| {
                s.key != ident.key && !s.closed && suffix_of(&s.key) == Some(suffix)
            })
        })
        .collect()
}

fn find_fork_target(
    w: &World,
    pi: usize,
    norm: &Normalized,
    ident: &Identity,
    digests: &[String],
) -> Option<u64> {
    if ident.scope != Scope::Subagent || norm.stream {
        return None;
    }
    let pool = match w.personas[pi].session(&ident.key) {
        Some(own) => vec![own],
        None => rekey_candidates(w, pi, ident),
    };
    pool.into_iter()
        .find(|sid| matches!(classify(w, *sid, digests, norm, ident), Some(Cls::Fork)))
}

fn adopt_rekeyed(
    w: &mut World,
    pi: usize,
    norm: &Normalized,
    ident: &Identity,
    digests: &[String],
) -> Option<u64> {
    let mut ids: HashSet<String> = HashSet::new();
    for m in &norm.msgs {
        if js::prop(Some(m), "role") != Some(&Value::from("assistant")) {
            continue;
        }
        let Some(Value::Array(c)) = js::prop(Some(m), "content") else {
            continue;
        };
        for b in c {
            if js::prop(Some(b), "type") == Some(&Value::from("tool_use")) {
                if let Some(id) = js::prop(Some(b), "id").filter(|v| js::truthy(Some(v))) {
                    ids.insert(id_key(id));
                }
            }
        }
    }
    if ids.is_empty() {
        return None;
    }
    let sid = rekey_candidates(w, pi, ident).into_iter().find(|sid| {
        matches!(
            classify(w, *sid, digests, norm, ident),
            Some(Cls::Continues)
        ) && w.sessions[sid].delivered.iter().any(|d| ids.contains(d))
    })?;
    let s = w.sessions.get_mut(&sid)?;
    let from = std::mem::replace(&mut s.key, ident.key.clone());
    s.outbox_key = outbox_key_of(&ident.key, ident.weak);
    let tid = s.thread_id.clone().unwrap_or(Value::Null);
    let p = &mut w.personas[pi];
    p.sessions.retain(|(k, _)| *k != from);
    p.set_session(&ident.key, sid);
    let name = p.name.clone();
    w.log(
        "session-rekeyed",
        json!({ "persona": name, "from": from, "to": ident.key, "threadId": tid }),
    );
    Some(sid)
}

fn new_session(w: &mut World, pi: usize, key: &str) -> u64 {
    let sid = w.id();
    w.sessions.insert(sid, Session::new(pi, key.to_string()));
    w.personas[pi].set_session(key, sid);
    sid
}

fn dispose_session(
    w: &mut World,
    pi: usize,
    norm: &Normalized,
    ident: &Identity,
    digests: Vec<String>,
) -> (u64, Mode) {
    let key = ident.key.as_str();
    let name = w.personas[pi].name.clone();
    let mut sid = w.personas[pi].session(key);
    if sid.is_none() {
        sid = adopt_rekeyed(w, pi, norm, ident, &digests);
    }
    let cls = sid.and_then(|s| classify(w, s, &digests, norm, ident));
    let (sid, mode) = match (sid, cls) {
        (Some(old), Some(Cls::Diverged { at, step })) => {
            let s = &w.sessions[&old];
            let mut row = Map::new();
            row.insert("persona".into(), Value::from(name.as_str()));
            row.insert("key".into(), Value::from(key));
            row.insert("seen".into(), Value::from(s.msg_count));
            row.insert("now".into(), Value::from(norm.msgs.len()));
            row.insert(
                "threadId".into(),
                s.thread_id.clone().unwrap_or(Value::Null),
            );
            if let Some(a) = at {
                row.insert("at".into(), Value::from(a));
            }
            if let Some(st) = step {
                row.insert("step".into(), Value::from(st));
            }
            w.log("rewind", Value::Object(row));
            persona::interrupt(w, pi, old, "rewind");
            persona::drop_session(w, pi, old);
            (new_session(w, pi, key), Mode::NewThreadAfterRewind)
        }
        (Some(s), Some(_)) => {
            let sess = &w.sessions[&s];
            let tr = &norm.tool_results;
            let echoes = || {
                tr.iter().any(|t| {
                    js::prop(Some(t), "tool_use_id")
                        .is_some_and(|id| sess.delivered.contains(&id_key(id)))
                })
            };
            if !tr.is_empty() && (!sess.parked.is_empty() || (sess.turn_open && echoes())) {
                (s, Mode::ToolResult)
            } else {
                if !sess.parked.is_empty() {
                    let n = sess.parked.len();
                    w.log(
                        "dropped-tool-calls",
                        json!({ "persona": name, "key": key, "n": n }),
                    );
                    session::abandon_parked(w, s, "no result returned by the client");
                }
                (s, Mode::NextTurn)
            }
        }
        _ => (new_session(w, pi, key), Mode::NewThread),
    };
    if let Some(s) = w.sessions.get_mut(&sid) {
        s.msg_count = norm.msgs.len();
        s.hist = digests;
        s.outbox_key = outbox_key_of(key, ident.weak);
    }
    (sid, mode)
}

/// `nTools: (body.tools || []).length` — `None` when `.length` is undefined.
fn n_tools(body: &Value) -> Option<Value> {
    let t = js::prop(Some(body), "tools");
    if !js::truthy(t) {
        return Some(Value::from(0));
    }
    match t {
        Some(Value::Array(a)) => Some(Value::from(a.len())),
        Some(Value::String(s)) => Some(Value::from(js::utf16_len(s))),
        _ => None,
    }
}

fn effort_val(ident: &Identity) -> Option<Value> {
    ident.eff.effort.as_deref().map(Value::from)
}

/// One `http` row per request (owner, joiner, replay), and the outbox rows for a turn.
fn log_http(
    w: &mut World,
    rc: &Rc,
    stop_reason: &Value,
    tool_calls: &Value,
    usage: &Value,
    usage_raw: Option<&Value>,
    extra: Vec<(&str, Value)>,
) {
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
    put("threadId", tid.clone().unwrap_or(Value::Null));
    put("stop_reason", stop_reason.clone());
    put("nMessages", Value::from(n.msgs.len()));
    put(
        "nSystemRole",
        Value::from(n.all_msgs.len() as i64 - n.msgs.len() as i64),
    );
    if let Some(t) = n_tools(&n.body) {
        put("nTools", t);
    }
    put("toolCalls", tool_calls.clone());
    put("usage", usage_out(Some(usage)));
    put(
        "usageCodex",
        js::present(usage_raw).unwrap_or(usage).clone(),
    );
    put("usageTotalCodex", total.clone().unwrap_or(Value::Null));
    put("headers", Value::Object(redact_headers(&rc.headers)));
    let joined = extra.iter().any(|(k, _)| *k == "joinedTo");
    for (k, v) in extra {
        put(k, v);
    }
    w.log("http", Value::Object(row));
    if joined {
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
        thread_id: Some(tid.unwrap_or(Value::Null)),
        usage_total: total.as_ref(),
        model: Some(Value::from(rc.ident.parsed.model.as_str())),
        effort: effort_val(&rc.ident),
        stop_reason: Some(stop_reason.clone()),
    });
}

/// A codex context overflow becomes `prompt is too long: N tokens > M maximum`. M is the
/// thread's `modelContextWindow` (else `SIDECAR_CONTEXT_WINDOW_TOKENS`); N is the request's
/// `count_tokens` estimate, floored at M+1 by `prompt_too_long`. Never retried.
fn overflow_shape(w: &World, rc: &Rc, e: SidecarError) -> SidecarError {
    if !e.is_context_overflow() {
        return e;
    }
    let window = w
        .sessions
        .get(&rc.sid)
        .and_then(|s| js::present(s.context_window.as_ref()))
        .map(|v| js::to_number(Some(v)))
        .filter(|n| n.is_finite() && *n > 0.0)
        .unwrap_or(w.cfg.context_window_tokens as f64);
    let estimate = crate::protocol::count_tokens_estimate(&rc.norm.body);
    crate::errors::prompt_too_long(&e, estimate, window)
}

fn http_error_row(rc: &Rc, e: &SidecarError, joined: Option<&Value>) -> Value {
    let mut row = json!({
        "persona": rc.persona, "key": rc.ident.key, "reqId": rc.req_id, "mode": rc.mode.as_str(),
        "status": e.status, "type": e.anthropic_type, "message": e.message,
        "headers": redact_headers(&rc.headers),
    });
    if let (Some(j), Some(o)) = (joined, row.as_object_mut()) {
        o.insert("joinedTo".into(), j.clone());
    }
    if let (Some(u), Some(o)) = (
        e.extra.as_ref().and_then(|x| x.get("upstreamMessage")),
        row.as_object_mut(),
    ) {
        o.insert("upstreamMessage".into(), u.clone());
    }
    row
}

// --------------------------------------------------------- REPLAY outcome ----

fn emit_replay(w: &mut World, rc: &Rc, lo: &LastOutcome) {
    w.log(
        "replayed-outcome",
        json!({ "persona": rc.persona, "key": rc.ident.key, "reqId": rc.req_id, "ownerReqId": lo.req_id, "reqDigest": short_digest(&rc.token.digest) }),
    );
    let lag = w.sessions[&rc.sid].lag_usage();
    sinks::start(w, rc.sink, &lag);
    for b in &lo.blocks {
        if js::prop(Some(b), "type") == Some(&Value::from("text")) {
            let t = js::prop(Some(b), "text")
                .and_then(Value::as_str)
                .unwrap_or("");
            sinks::text_delta(w, rc.sink, t);
        } else {
            sinks::tool_use(w, rc.sink, b);
        }
    }
    sinks::finish(w, rc.sink, &lo.stop_reason, &lo.usage);
    log_http(
        w,
        rc,
        &lo.stop_reason,
        &lo.tool_calls,
        &lo.usage,
        None,
        Vec::new(),
    );
}

// ----------------------------------------------------------- JOIN outcome ----

async fn run_as_joiner(me: &Shared, rc: &Rc, owner: u64, owner_req: Value) {
    let (jid, rx) = {
        let mut w = lock(me);
        let w = &mut *w;
        w.log(
            "joined-request",
            json!({ "persona": rc.persona, "key": rc.ident.key, "reqId": rc.req_id, "ownerReqId": owner_req, "mode": rc.mode.as_str(), "stream": rc.norm.stream, "reqDigest": short_digest(&rc.token.digest) }),
        );
        let (tx, rx) = tokio::sync::oneshot::channel();
        let jid = w.id();
        w.joiners.insert(
            jid,
            Joiner {
                owner,
                sink: rc.sink,
                settled: false,
                tx: Some(tx),
            },
        );
        let (lag, missed) = {
            let s = &w.sessions[&rc.sid];
            (s.lag_usage(), s.turn_text.concat())
        };
        sinks::start(w, rc.sink, &lag);
        if !missed.is_empty() {
            sinks::text_delta(w, rc.sink, &missed);
        }
        if let Some(s) = w.sessions.get_mut(&rc.sid) {
            s.joiners.push(jid);
        }
        if let Some(r) = w.sinks.get_mut(&rc.sink) {
            r.hook = Some(CloseHook::Joiner {
                sid: rc.sid,
                joiner: jid,
                sink: rc.sink,
                persona: rc.pi,
            });
        }
        (jid, rx)
    };
    let o = rx.await.unwrap_or(JoinOutcome::Closed);
    let mut w = lock(me);
    let w = &mut *w;
    match o {
        JoinOutcome::Closed => {}
        JoinOutcome::Error(e) => {
            let row = http_error_row(rc, &e, Some(&owner_req));
            w.log("http-error", row);
        }
        JoinOutcome::Done {
            stop_reason,
            tool_calls,
            usage,
        } => log_http(
            w,
            rc,
            &stop_reason,
            &tool_calls,
            &usage,
            None,
            vec![("joinedTo", owner_req.clone())],
        ),
    }
    remove_joiner(w, rc.sid, jid);
    w.joiners.remove(&jid);
    if let Some(r) = w.sinks.get_mut(&rc.sink) {
        r.hook = None;
    }
    w.sinks.remove(&rc.sink);
    w.gc_sessions();
}

/// `onJoinerClose`.
pub fn on_joiner_close(w: &mut World, sid: u64, jid: u64, sink: u64, pi: usize) {
    if sinks::sink_done(w, sink) {
        return;
    }
    sinks::abort(w, sink);
    remove_joiner(w, sid, jid);
    let owner = w.joiners.get(&jid).map(|j| j.owner);
    let gone = w.sessions.get(&sid).is_some_and(|s| s.owner_gone);
    if gone && joiners_of(w, sid, owner).is_empty() {
        persona::interrupt(w, pi, sid, "client disconnect");
        persona::drop_session(w, pi, sid);
        session::settle(
            w,
            sid,
            Outcome::Error(err(499, "api_error", "client disconnected")),
        );
    }
    settle_joiner(w, jid, JoinOutcome::Closed);
}

// ---------------------------------------------------------- DRIVE the turn ----

fn supersede(w: &mut World, rc: &Rc, old_id: u64, old: &Token) {
    let t = &rc.token;
    w.log(
        "superseded-request",
        json!({
            "persona": rc.persona, "key": rc.ident.key, "reqId": rc.req_id, "supersededReqId": old.req_id,
            "mode": rc.mode.as_str(), "nMessages": rc.norm.msgs.len(), "ownerNMessages": old.n_messages,
            "reqDigest": short_digest(&t.digest), "ownerDigest": short_digest(&old.digest),
            "parts": t.parts, "ownerParts": old.parts,
            "msgDiff": msg_diff(Some(&t.msg_shape), Some(&old.msg_shape)).unwrap_or(Value::Null),
        }),
    );
    for j in joiners_of(w, rc.sid, Some(old_id)) {
        remove_joiner(w, rc.sid, j);
        let e = superseded();
        if let Some(sk) = w.joiners.get(&j).map(|j| j.sink) {
            sinks::error(w, sk, &e);
        }
        settle_joiner(w, j, JoinOutcome::Error(e));
    }
    session::preempt(w, rc.sid, superseded());
}

/// `onClose` for the owner's response.
pub fn on_owner_close(w: &mut World, sid: u64, token: u64, sink: u64, pi: usize) {
    if sinks::sink_done(w, sink) {
        return;
    }
    if let Some(s) = w.sinks.get_mut(&sink) {
        s.closed_by_client = true;
    }
    sinks::abort(w, sink);
    if w.sessions.get(&sid).and_then(|s| s.owner_id()) != Some(token) {
        return;
    }
    let mine = joiners_of(w, sid, Some(token));
    if let Some(first) = mine.first() {
        let js = w.joiners.get(first).map(|j| j.sink);
        if let Some(s) = w.sessions.get_mut(&sid) {
            s.owner_gone = true;
        }
        detach(w, sid, Some(sink));
        if let Some(js) = js {
            attach(w, sid, js);
        }
        return;
    }
    persona::interrupt(w, pi, sid, "client disconnect");
    persona::drop_session(w, pi, sid);
    session::settle(
        w,
        sid,
        Outcome::Error(err(499, "api_error", "client disconnected")),
    );
}

fn closed_by_client(w: &World, sink: u64) -> bool {
    w.sinks.get(&sink).is_some_and(|s| s.closed_by_client)
}

fn is_owner(w: &World, rc: &Rc) -> bool {
    w.sessions.get(&rc.sid).and_then(|s| s.owner_id()) == Some(rc.token_id)
}

fn audience_of(w: &World, sink: u64, joiners: &[u64]) -> Vec<u64> {
    let mut out: Vec<u64> = Vec::new();
    let all = std::iter::once(sink).chain(
        joiners
            .iter()
            .filter_map(|j| w.joiners.get(j).map(|j| j.sink)),
    );
    for sk in all {
        if !sinks::sink_done(w, sk) && !out.contains(&sk) {
            out.push(sk);
        }
    }
    out
}

/// The try-block of `runAsOwner`; `Ok(())` covers both emission and the silent client abort.
async fn drive(
    me: &Shared,
    rc: &Rc,
    joiners: &mut Vec<u64>,
    deduped: &mut usize,
) -> Result<(), SidecarError> {
    if rc.mode.is_new_thread() {
        start_thread(me, rc).await?;
    }
    if rc.mode == Mode::ToolResult {
        let mut w = lock(me);
        *deduped = deliver_tool_results(&mut w, rc);
    } else {
        start_turn(me, rc).await?;
    }
    let (rx, t) = {
        let mut w = lock(me);
        let w = &mut *w;
        still_mine(w, rc)?;
        let lag = w.sessions[&rc.sid].lag_usage();
        sinks::start(w, rc.sink, &lag);
        let t = w.cfg.turn_timeout_ms;
        (session::wait_outcome(w, rc.sid), t)
    };
    let out = session::await_outcome(me, rc.sid, rx, std::time::Duration::from_millis(t)).await;
    let mut w = lock(me);
    let w = &mut *w;
    if is_owner(w, rc) {
        *joiners = joiners_of(w, rc.sid, Some(rc.token_id));
        for j in joiners.iter() {
            remove_joiner(w, rc.sid, *j);
        }
        detach(w, rc.sid, None);
    }
    detach(w, rc.sid, Some(rc.sink));
    let audience = audience_of(w, rc.sink, joiners);
    if closed_by_client(w, rc.sink) && joiners.is_empty() {
        return Ok(());
    }
    if let Outcome::Error(e) = out {
        return Err(e);
    }
    emit_outcome(w, rc, out, &audience, joiners, *deduped);
    Ok(())
}

async fn run_as_owner(me: &Shared, rc: &Rc, supersedes: Option<(u64, Box<Token>)>) {
    {
        let mut w = lock(me);
        let w = &mut *w;
        if let Some((oid, old)) = &supersedes {
            supersede(w, rc, *oid, old);
        }
        if let Some(s) = w.sessions.get_mut(&rc.sid) {
            s.owner = Some(OwnerTok {
                id: rc.token_id,
                token: rc.token.clone(),
            });
            s.owner_gone = false;
            s.turn_text.clear();
        }
        attach(w, rc.sid, rc.sink);
        if let Some(r) = w.sinks.get_mut(&rc.sink) {
            r.hook = Some(CloseHook::Owner {
                sid: rc.sid,
                token: rc.token_id,
                sink: rc.sink,
                persona: rc.pi,
            });
        }
    }
    let mut joiners: Vec<u64> = Vec::new();
    let mut deduped = 0usize;
    let r = drive(me, rc, &mut joiners, &mut deduped).await;
    let mut w = lock(me);
    let w = &mut *w;
    let r = r.map_err(|e| overflow_shape(w, rc, e));
    if let Err(e) = r {
        // catch
        if is_owner(w, rc) {
            for j in joiners_of(w, rc.sid, Some(rc.token_id)) {
                joiners.push(j);
                remove_joiner(w, rc.sid, j);
            }
            if let Some(s) = w.sessions.get_mut(&rc.sid) {
                s.turn_open = false;
            }
            detach(w, rc.sid, None);
        }
        detach(w, rc.sid, Some(rc.sink));
        for j in &joiners {
            settle_joiner(w, *j, JoinOutcome::Error(e.clone()));
        }
        let audience = audience_of(w, rc.sink, &joiners);
        if !(closed_by_client(w, rc.sink) && joiners.is_empty()) {
            let row = http_error_row(rc, &e, None);
            w.log("http-error", row);
            for sk in audience {
                sinks::error(w, sk, &e);
            }
        }
    }
    // finally
    if is_owner(w, rc) {
        if let Some(s) = w.sessions.get_mut(&rc.sid) {
            s.owner = None;
            s.owner_gone = false;
        }
    }
    if let Some(r) = w.sinks.get_mut(&rc.sink) {
        r.hook = None;
    }
    let stalled = err(
        502,
        "api_error",
        "the turn driving this joined request ended without an outcome",
    );
    let mut all = joiners_of(w, rc.sid, Some(rc.token_id));
    all.extend(joiners.iter().copied());
    for j in all {
        remove_joiner(w, rc.sid, j);
        let pending = w.joiners.get(&j).map(|j| (j.settled, j.sink));
        if let Some((false, sk)) = pending {
            sinks::error(w, sk, &stalled);
            settle_joiner(w, j, JoinOutcome::Error(stalled.clone()));
        }
    }
    w.sinks.remove(&rc.sink);
    w.gc_sessions();
}

// -------------------------------------------------------- phase 4: emission --

fn tool_block(call: &Value) -> Value {
    let p = js::prop(Some(call), "params");
    let mut b = Map::new();
    b.insert("type".into(), Value::from("tool_use"));
    if let Some(id) = js::prop(p, "callId") {
        b.insert("id".into(), id.clone());
    }
    if let Some(n) = js::prop(p, "tool") {
        b.insert("name".into(), n.clone());
    }
    b.insert(
        "input".into(),
        js::present(js::prop(p, "arguments"))
            .cloned()
            .unwrap_or(json!({})),
    );
    Value::Object(b)
}

fn emit_outcome(
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
            .map(|c| {
                js::prop(js::prop(Some(c), "params"), "tool")
                    .cloned()
                    .unwrap_or(Value::Null)
            })
            .collect(),
    );
    let blocks: Vec<Value> = batch.iter().map(tool_block).collect();
    if is_tool {
        for (call, block) in batch.iter().zip(&blocks) {
            let id = js::prop(js::prop(Some(call), "params"), "callId")
                .cloned()
                .unwrap_or(Value::Null);
            if let Some(s) = w.sessions.get_mut(&rc.sid) {
                s.delivered.insert(id_key(&id));
            }
            for sk in audience {
                sinks::tool_use(w, *sk, block);
            }
        }
        if batch.len() > 1 {
            let tid = w.sessions[&rc.sid].thread_id.clone().unwrap_or(Value::Null);
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
    let (window, src) = match js::present(s.context_window.as_ref()) {
        Some(v) => (v.clone(), "app-server"),
        None => (Value::from(w.cfg.context_window_tokens), "env"),
    };
    let tid = s.thread_id.clone().unwrap_or(Value::Null);
    let clamped = clamp_usage(Some(&usage_raw), js::to_number(Some(&window)));
    let usage = clamped.clone().unwrap_or_else(|| usage_raw.clone());
    if let Some(c) = &clamped {
        w.log(
            "usage-clamped",
            json!({ "persona": rc.persona, "key": rc.ident.key, "threadId": tid, "window": window,
                    "windowSource": src, "raw": usage_raw, "clamped": usage_out(Some(c)) }),
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
            req_id: Value::from(rc.req_id.as_str()),
            digest: rc.token.digest.clone(),
            stop_reason: stop.clone(),
            tool_calls: tool_calls.clone(),
            usage: usage.clone(),
            blocks: lb,
        });
    }
    let extra = if deduped > 0 {
        vec![("dedupedToolResults", Value::from(deduped))]
    } else {
        Vec::new()
    };
    log_http(w, rc, &stop, &tool_calls, &usage, Some(&usage_raw), extra);
    for j in joiners {
        settle_joiner(
            w,
            *j,
            JoinOutcome::Done {
                stop_reason: stop.clone(),
                tool_calls: tool_calls.clone(),
                usage: usage.clone(),
            },
        );
    }
}
