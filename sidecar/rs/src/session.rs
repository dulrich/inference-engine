//! `session.mjs` — one codex thread's state, and the outcome protocol over it
//! (`wait_outcome` / `settle` / `drain`). Sessions live in `World::sessions` by id; every
//! method takes the world because, as in Node, a session reaches into its persona.

use crate::config::INBAND_ITEM_TYPES;
use crate::errors::{map_upstream_error, SidecarError};
use crate::js;
use crate::outbox_events::OutboxKey;
use crate::persona;
use crate::protocol::zero_usage;
use crate::request::Token;
use crate::sidecar::{err, lock, with_world, Shared, World};
use crate::sinks;
use serde_json::{json, Value};
use std::collections::HashSet;
use tokio::sync::oneshot;
use tokio::task::AbortHandle;
use tokio::time::{Duration, Instant};

#[derive(Debug, Clone)]
pub enum Outcome {
    Error(SidecarError),
    /// The batched `item/tool/call` request frames.
    ToolUse(Vec<Value>),
    EndTurn,
}

/// The request currently driving a session (Node compares tokens by identity: `id`).
pub struct OwnerTok {
    pub id: u64,
    pub token: Token,
}

pub struct LastOutcome {
    pub req_id: Value,
    pub digest: String,
    pub stop_reason: Value,
    pub tool_calls: Value,
    pub usage: Value,
    pub blocks: Vec<Value>,
}

pub enum JoinOutcome {
    Closed,
    Error(SidecarError),
    Done {
        stop_reason: Value,
        tool_calls: Value,
        usage: Value,
    },
}

pub struct Joiner {
    /// The owner request token id this joiner joined (`ownerToken`).
    pub owner: u64,
    pub sink: u64,
    pub settled: bool,
    pub tx: Option<oneshot::Sender<JoinOutcome>>,
}

pub enum Frame {
    ChildExit(SidecarError),
    ToolCall(Value),
    Note(Value),
}

pub struct Session {
    pub persona: usize,
    pub key: String,
    pub thread_id: Option<Value>,
    pub turn_id: Option<Value>,
    pub model: Option<String>,
    pub last_effort: Option<Option<String>>,
    /// callId -> JSON-RPC request id, in insertion order (a JS `Map`).
    pub parked: Vec<(Value, Value)>,
    pub delivered: HashSet<String>,
    pub turn_open: bool,
    pub batch: Vec<Value>,
    /// The batch debounce: when the open batch settles (moved by each new call), and the
    /// one task that waits for it.
    pub batch_due: Option<Instant>,
    pub batch_timer: Option<AbortHandle>,
    pub msg_count: usize,
    pub hist: Vec<String>,
    pub delta_items: Vec<Value>,
    pub usage_last: Option<Value>,
    pub usage_total: Option<Value>,
    pub context_window: Option<Value>,
    pub outbox_key: Option<OutboxKey>,
    pub sink: Option<u64>,
    pub owner: Option<OwnerTok>,
    pub owner_gone: bool,
    /// Joiner ids, in insertion order (a JS `Set`).
    pub joiners: Vec<u64>,
    pub turn_text: Vec<String>,
    pub last_outcome: Option<LastOutcome>,
    pub pending_outcomes: Vec<Outcome>,
    pub resolve: Option<oneshot::Sender<Outcome>>,
    pub closed: bool,
}

/// JS `SameValueZero` for the primitive ids a `Map`/`Set` is keyed by (objects never match).
pub fn same_value(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Object(_), _) | (Value::Array(_), _) => false,
        (Value::Number(x), Value::Number(y)) => x.as_f64() == y.as_f64(),
        _ => a == b,
    }
}

/// The `delivered` key of an id (strings as-is; shared with `request::disposition_of`).
pub fn id_key(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => js::js_string(other),
    }
}

impl Session {
    pub fn new(persona: usize, key: String) -> Self {
        Self {
            persona,
            key,
            thread_id: None,
            turn_id: None,
            model: None,
            last_effort: None,
            parked: Vec::new(),
            delivered: HashSet::new(),
            turn_open: false,
            batch: Vec::new(),
            batch_due: None,
            batch_timer: None,
            msg_count: 0,
            hist: Vec::new(),
            delta_items: Vec::new(),
            usage_last: None,
            usage_total: None,
            context_window: None,
            outbox_key: None,
            sink: None,
            owner: None,
            owner_gone: false,
            joiners: Vec::new(),
            turn_text: Vec::new(),
            last_outcome: None,
            pending_outcomes: Vec::new(),
            resolve: None,
            closed: false,
        }
    }

    pub fn owner_id(&self) -> Option<u64> {
        self.owner.as_ref().map(|o| o.id)
    }

    /// `reconcile()`: the latest `tokenUsage.last`, each field `max(0, v ?? 0)`.
    pub fn reconcile(&self) -> Value {
        let z = zero_usage();
        let mut out = serde_json::Map::new();
        for (k, _) in z.as_object().into_iter().flatten() {
            let v = js::present(js::prop(self.usage_last.as_ref(), k))
                .cloned()
                .unwrap_or(Value::from(0));
            let n = js::to_number(Some(&v));
            let r = if n.is_nan() { n } else { n.max(0.0) };
            out.insert(k.clone(), js::num(r));
        }
        Value::Object(out)
    }

    pub fn lag_usage(&self) -> Value {
        self.usage_last.clone().unwrap_or_else(zero_usage)
    }

    pub fn parked_get(&self, call: &Value) -> Option<usize> {
        self.parked.iter().position(|(k, _)| same_value(k, call))
    }
}

/// `joinersOf(owner)`.
pub fn joiners_of(w: &World, sid: u64, owner: Option<u64>) -> Vec<u64> {
    let Some(owner) = owner else {
        return Vec::new();
    };
    let Some(s) = w.sessions.get(&sid) else {
        return Vec::new();
    };
    s.joiners
        .iter()
        .copied()
        .filter(|j| w.joiners.get(j).is_some_and(|j| j.owner == owner))
        .collect()
}

pub fn remove_joiner(w: &mut World, sid: u64, j: u64) {
    if let Some(s) = w.sessions.get_mut(&sid) {
        s.joiners.retain(|x| *x != j);
    }
}

pub fn settle_joiner(w: &mut World, j: u64, o: JoinOutcome) {
    if let Some(jn) = w.joiners.get_mut(&j) {
        if jn.settled {
            return;
        }
        jn.settled = true;
        if let Some(tx) = jn.tx.take() {
            let _ = tx.send(o);
        }
    }
}

pub fn attach(w: &mut World, sid: u64, sink: u64) {
    if let Some(s) = w.sessions.get_mut(&sid) {
        s.sink = Some(sink);
    }
}

/// `detach(sink)`: a `None` sink detaches whatever is attached.
pub fn detach(w: &mut World, sid: u64, sink: Option<u64>) {
    if let Some(s) = w.sessions.get_mut(&sid) {
        if sink.is_none() || s.sink == sink {
            s.sink = None;
        }
    }
}

/// `emitText(t)`: to the owner's sink, every joiner of the current owner, and `turnText`.
pub fn emit_text(w: &mut World, sid: u64, t: &str) {
    if t.is_empty() {
        return;
    }
    let Some(s) = w.sessions.get_mut(&sid) else {
        return;
    };
    s.turn_text.push(t.to_string());
    let sink = s.sink;
    let owner = s.owner_id();
    if let Some(sk) = sink {
        sinks::text_delta(w, sk, t);
    }
    for j in joiners_of(w, sid, owner) {
        let js = w.joiners.get(&j).map(|j| j.sink);
        if let Some(js) = js.filter(|js| Some(*js) != sink) {
            sinks::text_delta(w, js, t);
        }
    }
}

/// `resolveOutcome?.(o)`: hand one outcome to the parked request and clear its timer.
pub fn resolve_outcome(w: &mut World, sid: u64, o: Outcome) {
    if let Some(s) = w.sessions.get_mut(&sid) {
        if let Some(tx) = s.resolve.take() {
            let _ = tx.send(o);
        }
    }
}

pub fn preempt(w: &mut World, sid: u64, e: SidecarError) {
    resolve_outcome(w, sid, Outcome::Error(e));
}

fn turn_timed_out() -> Outcome {
    Outcome::Error(err(
        502,
        "api_error",
        "timed out waiting for the codex app-server",
    ))
}

/// `waitOutcome(timeoutMs)`, first half: park the request; `await_outcome` is the wait.
pub fn wait_outcome(w: &mut World, sid: u64) -> oneshot::Receiver<Outcome> {
    let (tx, rx) = oneshot::channel();
    let Some(s) = w.sessions.get_mut(&sid) else {
        let _ = tx.send(turn_timed_out());
        return rx;
    };
    s.resolve = Some(tx);
    drain(w, sid);
    rx
}

/// `waitOutcome(timeoutMs)`, second half: the outcome, or the turn timeout. On timeout the
/// parked resolver is withdrawn, unless an outcome landed in the same instant.
pub async fn await_outcome(
    me: &Shared,
    sid: u64,
    mut rx: oneshot::Receiver<Outcome>,
    timeout: Duration,
) -> Outcome {
    match tokio::time::timeout(timeout.max(Duration::from_millis(1)), &mut rx).await {
        Ok(r) => r.unwrap_or_else(|_| turn_timed_out()),
        Err(_) => {
            let mut w = lock(me);
            match rx.try_recv() {
                Ok(o) => return o,
                // Still open: the parked resolver is ours, withdraw it. Closed means a newer
                // request already replaced it — leave that one alone.
                Err(oneshot::error::TryRecvError::Empty) => {
                    if let Some(s) = w.sessions.get_mut(&sid) {
                        s.resolve = None;
                    }
                }
                Err(oneshot::error::TryRecvError::Closed) => {}
            }
            turn_timed_out()
        }
    }
}

/// `settle(outcome)`: outcomes queue in order; an error replaces whatever is queued.
pub fn settle(w: &mut World, sid: u64, o: Outcome) {
    let Some(s) = w.sessions.get_mut(&sid) else {
        return;
    };
    if matches!(o, Outcome::Error(_)) {
        s.pending_outcomes = vec![o];
    } else {
        s.pending_outcomes.push(o);
    }
    drain(w, sid);
}

pub fn drain(w: &mut World, sid: u64) {
    let Some(s) = w.sessions.get_mut(&sid) else {
        return;
    };
    if s.pending_outcomes.is_empty() || s.resolve.is_none() {
        return;
    }
    let o = s.pending_outcomes.remove(0);
    resolve_outcome(w, sid, o);
}

/// Arm (or push back) the batch debounce. One task per open batch: it sleeps to the due
/// time, re-reads it (a new call moves it), and settles once it is reached.
fn arm_batch(w: &mut World, sid: u64) {
    let me = w.shared();
    let after = Duration::from_millis(w.cfg.batch_ms.max(1));
    let Some(s) = w.sessions.get_mut(&sid) else {
        return;
    };
    s.batch_due = Some(Instant::now() + after);
    if s.batch_timer.is_some() {
        return;
    }
    let task = tokio::spawn(async move {
        loop {
            let due = with_world(&me, |w| w.sessions.get(&sid).and_then(|s| s.batch_due));
            let Some(Some(due)) = due else {
                return;
            };
            tokio::time::sleep_until(due).await;
            let fired = with_world(&me, |w| {
                let Some(s) = w.sessions.get_mut(&sid) else {
                    return true;
                };
                match s.batch_due {
                    Some(d) if d > Instant::now() => return false,
                    None => return true,
                    Some(_) => {}
                }
                s.batch_due = None;
                s.batch_timer = None;
                if s.batch.is_empty() {
                    return true;
                }
                let batch = std::mem::take(&mut s.batch);
                settle(w, sid, Outcome::ToolUse(batch));
                true
            });
            if fired != Some(false) {
                return;
            }
        }
    });
    s.batch_timer = Some(task.abort_handle());
}

fn params_of(m: &Value) -> Value {
    match js::prop(Some(m), "params") {
        Some(p) if js::truthy(Some(p)) => p.clone(),
        _ => json!({}),
    }
}

fn truthy_or(v: Option<&Value>, prev: &Option<Value>) -> Option<Value> {
    if js::truthy(v) {
        v.cloned()
    } else {
        prev.clone()
    }
}

fn nullish_or(v: Option<&Value>, prev: &Option<Value>) -> Option<Value> {
    match v {
        Some(x) if !x.is_null() => Some(x.clone()),
        _ => prev.clone(),
    }
}

/// codex's input-token fields: a usage figure whose every one of these is 0 (or absent) is
/// a compaction interim, not a real occupancy reading.
const INPUT_USAGE_FIELDS: &[&str] = &["inputTokens", "cachedInputTokens", "cacheWriteInputTokens"];

fn input_is_zero(u: Option<&Value>) -> bool {
    INPUT_USAGE_FIELDS.iter().all(|k| {
        let v = js::present(js::prop(u, k))
            .cloned()
            .unwrap_or(Value::from(0));
        let n = js::to_number(Some(&v));
        n.is_nan() || n <= 0.0
    })
}

/// A usage figure whose input fields are all zero never replaces a non-zero one: during a
/// compaction codex emits such an interim, and reporting it would read as an empty context.
fn usage_or_prev(v: Option<&Value>, prev: &Option<Value>) -> Option<Value> {
    match v {
        Some(x) if !x.is_null() => {
            if input_is_zero(Some(x)) && prev.is_some() && !input_is_zero(prev.as_ref()) {
                prev.clone()
            } else {
                Some(x.clone())
            }
        }
        _ => prev.clone(),
    }
}

fn take_usage(s: &mut Session, tu: &Value) {
    s.usage_last = usage_or_prev(tu.get("last"), &s.usage_last);
    s.usage_total = usage_or_prev(tu.get("total"), &s.usage_total);
    // A zero window would clamp the projection to 0, so it never replaces a known one either.
    s.context_window = match tu.get("modelContextWindow") {
        Some(x) if js::to_number(Some(x)) == 0.0 && s.context_window.is_some() => {
            s.context_window.clone()
        }
        v => nullish_or(v, &s.context_window),
    };
}

/// `onFrame(ev)`: every app-server frame for this thread.
pub fn on_frame(w: &mut World, sid: u64, ev: Frame) {
    let Some(s) = w.sessions.get_mut(&sid) else {
        return;
    };
    if s.closed {
        return;
    }
    let m = match ev {
        Frame::ChildExit(e) => {
            settle(w, sid, Outcome::Error(e));
            return;
        }
        Frame::ToolCall(rpc) => {
            let p = params_of(&rpc);
            s.turn_id = truthy_or(p.get("turnId"), &s.turn_id);
            let call = p.get("callId").cloned().unwrap_or(Value::Null);
            let id = rpc.get("id").cloned().unwrap_or(Value::Null);
            match s.parked_get(&call) {
                Some(i) => s.parked[i].1 = id,
                None => s.parked.push((call, id)),
            }
            s.batch.push(rpc);
            arm_batch(w, sid);
            return;
        }
        Frame::Note(m) => m,
    };
    let p = params_of(&m);
    let method = m.get("method").cloned().unwrap_or(Value::Null);
    let pi = s.persona;
    match method.as_str().unwrap_or("") {
        "turn/started" => {
            s.turn_id = truthy_or(js::prop(p.get("turn"), "id"), &s.turn_id);
        }
        "item/started" => {
            let ty = js::prop(p.get("item"), "type").cloned();
            s.turn_id = truthy_or(p.get("turnId"), &s.turn_id);
            if let Some(ty) = ty.filter(|t| js::truthy(Some(t))) {
                let inband = ty.as_str().is_some_and(|t| INBAND_ITEM_TYPES.contains(&t));
                if !inband {
                    persona::trip_boundary(w, pi, ty, Some(sid));
                }
            }
        }
        "item/agentMessage/delta" => {
            if let Some(d) = p
                .get("delta")
                .and_then(Value::as_str)
                .filter(|d| !d.is_empty())
            {
                if let Some(item) = p.get("itemId").filter(|v| js::truthy(Some(v))) {
                    if !s.delta_items.iter().any(|x| same_value(x, item)) {
                        s.delta_items.push(item.clone());
                    }
                }
                emit_text(w, sid, d);
            }
        }
        "item/completed" => {
            let item = p.get("item");
            let is_msg = js::prop(item, "type") == Some(&Value::from("agentMessage"));
            let text = js::prop(item, "text")
                .and_then(Value::as_str)
                .filter(|t| !t.is_empty());
            if let (true, Some(t)) = (is_msg, text) {
                let id = js::prop(item, "id").cloned().unwrap_or(Value::Null);
                let seen = !id.is_null() && s.delta_items.iter().any(|x| same_value(x, &id));
                if !seen {
                    emit_text(w, sid, t);
                }
            }
        }
        "thread/tokenUsage/updated" => {
            if let Some(tu) = p.get("tokenUsage").filter(|v| js::truthy(Some(v))) {
                take_usage(s, tu);
            }
        }
        "hook/started" | "mcpServer/startupStatus/updated" => {
            let tid = s.thread_id.clone().unwrap_or(Value::Null);
            let name = w.personas[pi].name.clone();
            w.log(
                "config-unverified",
                json!({ "persona": name, "threadId": tid, "stage": "runtime", "method": method }),
            );
            let msg = format!(
                "thread config not honoured: unexpected {}",
                method.as_str().unwrap_or("")
            );
            settle(w, sid, Outcome::Error(err(502, "api_error", msg)));
        }
        "error" => {
            let e = match p.get("error") {
                Some(v) if !v.is_null() => v.clone(),
                _ => p.clone(),
            };
            settle(
                w,
                sid,
                Outcome::Error(map_upstream_error(&e, "app-server error")),
            );
        }
        "turn/completed" => {
            let turn = match p.get("turn") {
                Some(t) if js::truthy(Some(t)) => t.clone(),
                _ => json!({}),
            };
            s.turn_id = truthy_or(turn.get("id"), &s.turn_id);
            if let Some(tu) = p.get("tokenUsage").filter(|v| js::truthy(Some(v))) {
                take_usage(s, tu);
            }
            let status = turn.get("status");
            if status == Some(&Value::from("failed")) || js::truthy(turn.get("error")) {
                let e = match turn.get("error") {
                    Some(v) if !v.is_null() => v.clone(),
                    _ => {
                        let st = status.map_or("undefined".to_string(), js::js_string);
                        json!({ "message": format!("turn {st}") })
                    }
                };
                settle(
                    w,
                    sid,
                    Outcome::Error(map_upstream_error(&e, "turn failed")),
                );
                return;
            }
            if s.batch_due.is_some() {
                return;
            }
            settle(w, sid, Outcome::EndTurn);
        }
        _ => {}
    }
}

/// `answerParked(callId, text, success)`.
pub fn answer_parked(w: &mut World, sid: u64, call: &Value, text: &str, success: bool) -> bool {
    let Some(s) = w.sessions.get_mut(&sid) else {
        return false;
    };
    let Some(i) = s.parked_get(call) else {
        return false;
    };
    let (_, id) = s.parked.remove(i);
    let pi = s.persona;
    let text = if text.is_empty() { "(empty)" } else { text };
    persona::send(
        w,
        pi,
        &json!({ "jsonrpc": "2.0", "id": id, "result": { "contentItems": [{ "type": "inputText", "text": text }], "success": success } }),
    );
    true
}

/// `abandonDelivered(reason)`: only calls the client has already been shown.
pub fn abandon_delivered(w: &mut World, sid: u64, reason: &str) {
    let calls: Vec<Value> = match w.sessions.get(&sid) {
        Some(s) => s
            .parked
            .iter()
            .filter(|(k, _)| s.delivered.contains(&id_key(k)))
            .map(|(k, _)| k.clone())
            .collect(),
        None => return,
    };
    for c in calls {
        answer_parked(w, sid, &c, reason, false);
    }
}

pub fn abandon_parked(w: &mut World, sid: u64, reason: &str) {
    let calls: Vec<Value> = match w.sessions.get(&sid) {
        Some(s) => s.parked.iter().map(|(k, _)| k.clone()).collect(),
        None => return,
    };
    for c in calls {
        answer_parked(w, sid, &c, reason, false);
    }
    if let Some(s) = w.sessions.get_mut(&sid) {
        s.batch.clear();
        s.batch_due = None;
        if let Some(t) = s.batch_timer.take() {
            t.abort();
        }
    }
}

pub fn close(w: &mut World, sid: u64, reason: &str) {
    let Some(s) = w.sessions.get_mut(&sid) else {
        return;
    };
    if s.closed {
        return;
    }
    s.closed = true;
    let reason = if reason.is_empty() { "aborted" } else { reason };
    abandon_parked(w, sid, reason);
    resolve_outcome(
        w,
        sid,
        Outcome::Error(err(409, "api_error", format!("session closed: {reason}"))),
    );
    let Some(s) = w.sessions.get_mut(&sid) else {
        return;
    };
    let (pi, tid) = (s.persona, s.thread_id.clone());
    if let Some(tid) = tid {
        w.personas[pi].bus.retain(|(k, _)| !same_value(k, &tid));
    }
}
