//! Session disposition (phase 2 of POST /<persona>/v1/messages): classify the request
//! against the persona's sessions, re-key or fork when the history allows, else open a new
//! session.

use crate::outbox_events::outbox_key_of;
use crate::persona;
use crate::protocol::Scope;
use crate::request::{
    canon, disposition_of, request_token, Content, ContentBlock, Disposition, Identity, Message,
    Mode, Normalized,
};
use crate::session::{self, id_key, Session};
use crate::sidecar::World;
use serde_json::{json, Map, Value};
use std::collections::HashSet;

enum Cls {
    Fork,
    Continues,
    Diverged {
        at: Option<usize>,
        step: Option<i64>,
    },
}

/// Classifies the request's history against one session's.
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
    let mut out: Vec<u64> = w.personas[pi]
        .sessions
        .values()
        .copied()
        .filter(|sid| {
            w.sessions.get(sid).is_some_and(|s| {
                s.key != ident.key && !s.closed && suffix_of(&s.key) == Some(suffix)
            })
        })
        .collect();
    // oldest session first (ids are monotonic), deterministic across map orders
    out.sort_unstable();
    out
}

pub(crate) fn find_fork_target(
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
    for m in norm.msgs.iter().filter(|m| m.role() == Some("assistant")) {
        let Some(Content::Blocks(c)) = &m.content else {
            continue;
        };
        for b in c {
            if let ContentBlock::ToolUse(t) = b {
                ids.extend(session::usable_id(t.id.as_ref()));
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
    let tid = s.thread_id.as_ref().map_or(Value::Null, Value::from);
    let p = &mut w.personas[pi];
    p.sessions.remove(&from);
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

/// An assistant message as comparable blocks: text (non-empty) and tool_use, decoration
/// (`cache_control`, unknown keys) dropped. `None` when it holds anything else.
fn answer_shape_of_blocks(blocks: &[Value]) -> Option<Vec<Value>> {
    let mut out = Vec::new();
    for b in blocks {
        match b.get("type").and_then(Value::as_str) {
            Some("text") => {
                let t = b.get("text").and_then(Value::as_str)?;
                if !t.is_empty() {
                    out.push(json!({ "type": "text", "text": t }));
                }
            }
            Some("tool_use") => out.push(canon(&json!({
                "type": "tool_use",
                "id": b.get("id").cloned().unwrap_or(Value::Null),
                "name": b.get("name").cloned().unwrap_or(Value::Null),
                "input": b.get("input").filter(|v| !v.is_null()).cloned().unwrap_or_else(|| json!({})),
            }))),
            _ => return None,
        }
    }
    Some(out)
}

fn answer_shape_of_message(m: &Message) -> Option<Vec<Value>> {
    if m.role() != Some("assistant") {
        return None;
    }
    match &m.content {
        Some(Content::Text(t)) => answer_shape_of_blocks(&[json!({ "type": "text", "text": t })]),
        Some(Content::Blocks(bs)) => {
            let vs: Vec<Value> = bs.iter().map(ContentBlock::to_value).collect();
            answer_shape_of_blocks(&vs)
        }
        _ => None,
    }
}

/// Weak key, `n > m`: the request's message at the session's boundary is the answer this
/// session last emitted (and no request is driving it). Not comparable means not owned.
fn owns_continuation(sess: &Session, norm: &Normalized) -> bool {
    let m = sess.hist.len();
    if m == 0 || sess.owner.is_some() {
        return false;
    }
    let Some(lo) = &sess.last_outcome else {
        return false;
    };
    let (Some(mine), Some(theirs)) = (
        answer_shape_of_blocks(&lo.blocks),
        norm.msgs.get(m).and_then(answer_shape_of_message),
    ) else {
        return false;
    };
    !mine.is_empty() && canon(&Value::Array(mine)) == canon(&Value::Array(theirs))
}

/// Weak key, `n == m`: the mode under which the request still shares the session — a
/// byte-identical twin of the in-flight owner joins it, a tool-result straggler replays.
/// `None` means it is an independent call and is isolated.
fn weak_twin_mode(w: &World, sid: u64, norm: &Normalized, ident: &Identity) -> Option<Mode> {
    let sess = w.sessions.get(&sid)?;
    let mode = match &sess.owner {
        Some(o) => o.token.mode,
        None if norm.tool_results().is_empty() => return None,
        None => Mode::ToolResult,
    };
    let token = request_token(String::new(), mode, norm, ident);
    match disposition_of(sess, &token) {
        Disposition::Join { .. } => Some(mode),
        Disposition::Replay { .. } => Some(Mode::NextTurn),
        Disposition::Drive { .. } => None,
    }
}

/// Weak key: give the request a fresh session (re-pointing the key) and leave the old one
/// to finish its in-flight work; it is retired once settled (`World::gc_sessions`).
fn isolate(w: &mut World, pi: usize, old: u64, norm: &Normalized, key: &str, reason: &str) -> u64 {
    let name = w.personas[pi].name.clone();
    let Some(s) = w.sessions.get_mut(&old) else {
        return new_session(w, pi, key);
    };
    s.displaced = true;
    let row = json!({
        "persona": name, "key": key, "reason": reason, "seen": s.hist.len(), "now": norm.msgs.len(),
        "inFlight": s.in_flight(), "threadId": s.thread_id.as_ref().map_or(Value::Null, Value::from),
    });
    w.log("weak-session-isolated", row);
    new_session(w, pi, key)
}

pub(crate) fn dispose_session(
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
    let mut forced: Option<Mode> = None;
    let cls = match (sid, cls) {
        (Some(old), Some(c)) if ident.weak => {
            let sess = &w.sessions[&old];
            let reason = match c {
                Cls::Continues if digests.len() == sess.hist.len() => {
                    match weak_twin_mode(w, old, norm, ident) {
                        Some(m) => {
                            forced = sess.owner.is_some().then_some(m);
                            None
                        }
                        None => Some("no-extension"),
                    }
                }
                Cls::Continues if !owns_continuation(sess, norm) => Some("not-owned"),
                Cls::Diverged { .. } if sess.in_flight() => Some("diverged-in-flight"),
                _ => None,
            };
            if let Some(reason) = reason {
                let sid = isolate(w, pi, old, norm, key, reason);
                return finish(w, sid, Mode::NewThread, norm, ident, digests);
            }
            Some(c)
        }
        (_, c) => c,
    };
    if let (Some(s), Some(m)) = (sid, forced) {
        return finish(w, s, m, norm, ident, digests);
    }
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
                s.thread_id.as_ref().map_or(Value::Null, Value::from),
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
            let (threadless, in_flight, seen) =
                (sess.thread_id.is_none(), sess.in_flight(), sess.hist.len());
            let tr = norm.tool_results();
            let echoes = || {
                tr.iter().any(|t| {
                    t.tool_use_id
                        .as_ref()
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
                if threadless {
                    // (a) no thread yet (its `thread/start` is still awaited, or failed): a
                    // next turn would carry a null threadId, so open the thread instead.
                    w.log(
                        "thread-guard",
                        json!({ "persona": name, "key": key, "seen": seen, "now": norm.msgs.len(), "inFlight": in_flight }),
                    );
                    (s, Mode::NewThread)
                } else {
                    (s, Mode::NextTurn)
                }
            }
        }
        _ => (new_session(w, pi, key), Mode::NewThread),
    };
    finish(w, sid, mode, norm, ident, digests)
}

fn finish(
    w: &mut World,
    sid: u64,
    mode: Mode,
    norm: &Normalized,
    ident: &Identity,
    digests: Vec<String>,
) -> (u64, Mode) {
    let key = ident.key.as_str();
    if let Some(s) = w.sessions.get_mut(&sid) {
        s.msg_count = norm.msgs.len();
        s.hist = digests;
        s.outbox_key = outbox_key_of(key, ident.weak);
    }
    (sid, mode)
}
