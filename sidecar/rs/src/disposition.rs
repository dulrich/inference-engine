//! Session disposition (phase 2 of POST /<persona>/v1/messages): classify the request
//! against the persona's sessions, re-key or fork when the history allows, else open a new
//! session.

use crate::outbox_events::outbox_key_of;
use crate::persona;
use crate::protocol::Scope;
use crate::request::{Content, ContentBlock, Identity, Mode, Normalized};
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
