//! POST /<persona>/v1/messages as the explicit four-phase sequence:
//! normalization, session disposition (replay | join | drive), turn execution, outcome
//! emission. Every exit writes one `http` (or `http-error`) row and settles every attached
//! connection.

use crate::disposition::{dispose_session, find_fork_target};
use crate::emission::{emit_outcome, emit_replay, http_error_row, log_http, HttpRow, Role};
use crate::errors::SidecarError;
use crate::persona;
use crate::protocol::{has_image_block, is_title_request, title_envelope, usage_out};
use crate::request::{
    disposition_of, message_digests, msg_diff, normalize_request, request_token, resolve_identity,
    short_digest, Disposition, Identity, Mode, Normalized, Request, Token,
};
use crate::session::{
    self, attach, detach, joiners_of, remove_joiner, settle_joiner, JoinOutcome, Joiner,
    LastOutcome, Outcome, OwnerTok,
};
use crate::sidecar::{err, lock, random_hex, Shared, World};
use crate::sinks::{self, message_skeleton, wire_model_id, with_keys, CloseHook, Reply, Sink};
use crate::turn::{deliver_tool_results, start_thread, start_turn, still_mine};
use axum::http::HeaderMap;
use serde_json::{json, Value};
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
    let norm = normalize_request(Request::parse(body)?, Some(headers.clone()));
    let mut w = lock(me);
    let w = &mut *w;
    let name = w.personas[pi].name.clone();
    if is_title_request(&norm.req, &norm.system_text, &norm.msgs) {
        w.log(
            "title-local",
            json!({ "persona": name, "stream": norm.stream, "envelope": title_envelope(&norm.msgs) }),
        );
        return Ok(Admit::Local(local_text(&name, &norm, "Session")));
    }
    if has_image_block(&norm.req, norm.all_msgs()) {
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
        return Ok(Admit::Local(local_text(&name, &norm, FORK_TEXT)));
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
        let wire = wire_model_id(&name, &norm.req.model_text());
        let Some(tx) = reply.take() else {
            return Ok(());
        };
        let sk = if norm.stream {
            Sink::sse(tx, &wire, w.cfg.ping_ms)
        } else {
            Sink::buffer(tx, &wire)
        };
        w.sinks.insert(sink, sk);
        let token = request_token(req_id.clone(), mode, &norm, &ident);
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
                    usage: outcome.usage,
                    blocks: outcome.blocks.clone(),
                };
                emit_replay(w, &rc, &lo);
                Plan::Done
            }
            Disposition::Join { owner } => Plan::Join {
                owner: owner.id,
                owner_req: Value::from(owner.token.req_id.as_str()),
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

fn local_text(persona: &str, norm: &Normalized, text: &str) -> Reply {
    let usage = usage_out(None);
    let model = wire_model_id(persona, &norm.req.model_text());
    if norm.stream {
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
            .map(|(t, o)| format!("event: {t}\ndata: {o}\n\n"))
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
        .and_then(|s| s.context_window)
        .filter(|n| *n > 0)
        .unwrap_or(w.cfg.context_window_tokens) as f64;
    let estimate = crate::protocol::count_tokens_estimate(&rc.norm.req, rc.norm.all_msgs()) as f64;
    crate::errors::prompt_too_long(&e, estimate, window)
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
            let role = Role::Joiner {
                owner_req: owner_req.clone(),
            };
            let row = http_error_row(rc, &e, &role);
            w.log("http-error", row);
        }
        JoinOutcome::Done {
            stop_reason,
            tool_calls,
            usage,
        } => log_http(
            w,
            rc,
            HttpRow {
                stop_reason: &stop_reason,
                tool_calls: &tool_calls,
                usage: &usage,
                usage_raw: None,
                role: Role::Joiner {
                    owner_req: owner_req.clone(),
                },
            },
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

/// A joiner's response closed.
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

/// The owner's response closed.
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

/// Removes the owner's joiners from the session and clears its sink; returns their ids.
fn take_joiners(w: &mut World, sid: u64, token_id: u64) -> Vec<u64> {
    let js = joiners_of(w, sid, Some(token_id));
    for j in &js {
        remove_joiner(w, sid, *j);
    }
    detach(w, sid, None);
    js
}

/// The owner's request path; `Ok(())` covers both emission and the silent client abort.
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
        *joiners = take_joiners(w, rc.sid, rc.token_id);
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
            if let Some(s) = w.sessions.get_mut(&rc.sid) {
                s.turn_open = false;
            }
            joiners.extend(take_joiners(w, rc.sid, rc.token_id));
        }
        detach(w, rc.sid, Some(rc.sink));
        for j in &joiners {
            settle_joiner(w, *j, JoinOutcome::Error(e.clone()));
        }
        let audience = audience_of(w, rc.sink, &joiners);
        if !(closed_by_client(w, rc.sink) && joiners.is_empty()) {
            let row = http_error_row(rc, &e, &Role::Owner { deduped: 0 });
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
