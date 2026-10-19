//! `sinks.mjs` — the two response transports (`BufferSink`, `SseSink`), the message envelope
//! they share, and the one-shot writers. A sink is the only thing that writes to a client.
//!
//! A `Sink` owns its response: the head goes to the waiting HTTP handler as a `Reply` (a
//! whole body, or a stream fed through a channel), and `destroyed` is set by the close
//! event (`http::CloseGuard`). An SSE sink owns one keep-alive task for its whole life.

use crate::codex::Usage;
use crate::errors::SidecarError;
use crate::protocol::usage_out;
use crate::sidecar::{random_hex, with_world, World};
use bytes::Bytes;
use serde_json::{json, Map, Value};
use tokio::sync::{mpsc, oneshot};
use tokio::task::AbortHandle;
use tokio::time::{Duration, Instant};

pub type Headers = Vec<(String, String)>;

pub enum Reply {
    Full {
        status: u16,
        headers: Headers,
        body: Option<String>,
    },
    Stream {
        status: u16,
        headers: Headers,
        rx: mpsc::UnboundedReceiver<Bytes>,
    },
}

impl Reply {
    /// A JSON body (`sendJson(res, status, obj, headers)`).
    pub fn json(status: u16, obj: &Value, extra: Headers) -> Self {
        let mut hs = json_headers();
        for (k, v) in extra {
            hs.retain(|(hk, _)| hk != &k);
            hs.push((k, v));
        }
        Reply::Full {
            status,
            headers: hs,
            body: Some(obj.to_string()),
        }
    }
    /// `sendError(res, err)`.
    pub fn error(err: &SidecarError) -> Self {
        Reply::json(err.status, &err.body(), retry_after_header(err))
    }
    /// A complete, already-known SSE body, sent as a stream.
    pub fn sse(text: String) -> Self {
        let (tx, rx) = mpsc::unbounded_channel();
        let _ = tx.send(Bytes::from(text));
        Reply::Stream {
            status: 200,
            headers: sse_headers(),
            rx,
        }
    }
}

/// What `res.on("close", fn)` registered (messages.mjs `onClose` / `onJoinerClose`).
#[derive(Clone)]
pub enum CloseHook {
    Owner {
        sid: u64,
        token: u64,
        sink: u64,
        persona: usize,
    },
    Joiner {
        sid: u64,
        joiner: u64,
        sink: u64,
        persona: usize,
    },
}

fn h(pairs: &[(&str, &str)]) -> Headers {
    pairs
        .iter()
        .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
        .collect()
}

pub fn json_headers() -> Headers {
    h(&[("content-type", "application/json")])
}

pub fn sse_headers() -> Headers {
    h(&[
        ("content-type", "text/event-stream"),
        ("cache-control", "no-cache"),
        ("connection", "keep-alive"),
    ])
}

fn retry_after_header(err: &SidecarError) -> Headers {
    match err.retry_after {
        None => Vec::new(),
        Some(v) => vec![("retry-after".to_string(), v.to_string())],
    }
}

/// `wireModelId(personaName, requested)`: re-prefix the persona the router stripped.
pub fn wire_model_id(persona: &str, requested: &str) -> String {
    let id = requested.to_string();
    if id.contains('/') {
        id
    } else {
        format!("{persona}/{id}")
    }
}

pub fn message_skeleton(model: &str) -> Value {
    json!({
        "id": format!("msg_{}", random_hex(12)),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": null,
        "stop_sequence": null,
    })
}

/// `{...msg, k: v}` — spread then override, JS key order.
pub fn with_keys(msg: &Value, kv: Vec<(&str, Value)>) -> Value {
    let mut m: Map<String, Value> = msg.as_object().cloned().unwrap_or_default();
    for (k, v) in kv {
        m.insert(k.to_string(), v);
    }
    Value::Object(m)
}

pub enum Kind {
    Buffer {
        text: String,
        blocks: Vec<Value>,
    },
    Sse {
        ping_ms: u64,
        index: u64,
        text_open: bool,
        any_block: bool,
        /// The last successful write; the keep-alive task pings `ping_ms` after it.
        last_write: Instant,
        keepalive: Option<AbortHandle>,
        pre_start: Vec<String>,
    },
}

pub struct Sink {
    reply: Option<oneshot::Sender<Reply>>,
    body: Option<mpsc::UnboundedSender<Bytes>>,
    ended: bool,
    pub destroyed: bool,
    pub hook: Option<CloseHook>,
    pub msg: Value,
    pub started: bool,
    pub done: bool,
    pub closed_by_client: bool,
    pub kind: Kind,
}

impl Drop for Sink {
    fn drop(&mut self) {
        if let Kind::Sse {
            keepalive: Some(k), ..
        } = &self.kind
        {
            k.abort();
        }
    }
}

impl Sink {
    fn new(reply: oneshot::Sender<Reply>, model: &str, kind: Kind) -> Self {
        Self {
            reply: Some(reply),
            body: None,
            ended: false,
            destroyed: false,
            hook: None,
            msg: message_skeleton(model),
            started: false,
            done: false,
            closed_by_client: false,
            kind,
        }
    }
    pub fn buffer(reply: oneshot::Sender<Reply>, model: &str) -> Self {
        Self::new(
            reply,
            model,
            Kind::Buffer {
                text: String::new(),
                blocks: Vec::new(),
            },
        )
    }
    pub fn sse(reply: oneshot::Sender<Reply>, model: &str, ping_ms: u64) -> Self {
        Self::new(
            reply,
            model,
            Kind::Sse {
                ping_ms,
                index: 0,
                text_open: false,
                any_block: false,
                last_write: Instant::now(),
                keepalive: None,
                pre_start: Vec::new(),
            },
        )
    }
    /// The client is gone or the response already ended (Node `res.writableEnded ||
    /// res.destroyed`).
    fn gone(&self) -> bool {
        self.ended
            || self.destroyed
            || self.reply.as_ref().is_some_and(oneshot::Sender::is_closed)
            || self
                .body
                .as_ref()
                .is_some_and(mpsc::UnboundedSender::is_closed)
    }
    /// `writeHead(status, headers)` + `end(body)`.
    fn full(&mut self, reply: Reply) {
        if let Some(tx) = self.reply.take() {
            let _ = tx.send(reply);
        }
        self.ended = true;
        self.body = None;
    }
    /// `writeHead(status, headers)` for a streamed body.
    fn stream_head(&mut self, status: u16, headers: Headers) {
        if let Some(tx) = self.reply.take() {
            let (btx, rx) = mpsc::unbounded_channel();
            if tx
                .send(Reply::Stream {
                    status,
                    headers,
                    rx,
                })
                .is_ok()
            {
                self.body = Some(btx);
            }
        }
    }
    /// `res.write(text)`; false when the client is gone.
    fn send(&mut self, text: String) -> bool {
        if self.ended || self.destroyed {
            return false;
        }
        match &self.body {
            Some(b) => b.send(Bytes::from(text)).is_ok(),
            None => false,
        }
    }
    fn end(&mut self) {
        if self.reply.is_some() {
            self.full(Reply::Full {
                status: 200,
                headers: Vec::new(),
                body: None,
            });
        }
        self.ended = true;
        self.body = None;
    }
}

pub fn sink_done(w: &World, id: u64) -> bool {
    w.sinks.get(&id).is_none_or(|s| s.done)
}

fn frame(t: &str, obj: &Value) -> String {
    format!("event: {t}\ndata: {obj}\n\n")
}

/// `SseSink.#raw(text)`.
fn raw(w: &mut World, id: u64, text: String) -> bool {
    let Some(s) = w.sinks.get_mut(&id) else {
        return false;
    };
    if s.done || s.gone() {
        return false;
    }
    if !s.started {
        if let Kind::Sse { pre_start, .. } = &mut s.kind {
            pre_start.push(text);
        }
        return true;
    }
    if s.send(text) {
        return true;
    }
    abort(w, id);
    false
}

fn write(w: &mut World, id: u64, t: &str, obj: Value) {
    if raw(w, id, frame(t, &obj)) {
        touch(w, id);
    }
}

/// Re-arm the keep-alive: the next ping is due `ping_ms` after this write.
fn touch(w: &mut World, id: u64) {
    if let Some(Kind::Sse { last_write, .. }) = w.sinks.get_mut(&id).map(|s| &mut s.kind) {
        *last_write = Instant::now();
    }
}

/// The one keep-alive task of a started SSE sink: it sleeps until `ping_ms` after the last
/// write, re-reads the deadline (writes move it), and pings only after real silence. It
/// ends when the sink is done or gone.
fn spawn_keepalive(w: &mut World, id: u64) {
    let me = w.shared();
    let Some(Kind::Sse {
        ping_ms, keepalive, ..
    }) = w.sinks.get_mut(&id).map(|s| &mut s.kind)
    else {
        return;
    };
    if keepalive.is_some() {
        return;
    }
    let every = Duration::from_millis((*ping_ms).max(1));
    let task = tokio::spawn(async move {
        loop {
            let due = with_world(&me, |w| match w.sinks.get(&id) {
                Some(s) if !s.done => match &s.kind {
                    Kind::Sse { last_write, .. } => Some(*last_write + every),
                    Kind::Buffer { .. } => None,
                },
                _ => None,
            });
            let Some(Some(due)) = due else {
                return;
            };
            tokio::time::sleep_until(due).await;
            let go_on = with_world(&me, |w| {
                let silent = match w.sinks.get(&id) {
                    Some(s) if !s.done => match &s.kind {
                        Kind::Sse { last_write, .. } => *last_write + every <= Instant::now(),
                        Kind::Buffer { .. } => return false,
                    },
                    _ => return false,
                };
                if !silent {
                    return true;
                }
                if raw(w, id, frame("ping", &json!({ "type": "ping" }))) {
                    touch(w, id);
                    return true;
                }
                false
            });
            if go_on != Some(true) {
                return;
            }
        }
    });
    *keepalive = Some(task.abort_handle());
}

fn clear_timer(s: &mut Sink) {
    if let Kind::Sse { keepalive, .. } = &mut s.kind {
        if let Some(k) = keepalive.take() {
            k.abort();
        }
    }
}

pub fn abort(w: &mut World, id: u64) {
    if let Some(s) = w.sinks.get_mut(&id) {
        s.done = true;
        clear_timer(s);
    }
}

pub fn start(w: &mut World, id: u64, usage: &Usage) {
    let Some(s) = w.sinks.get_mut(&id) else {
        return;
    };
    if s.started {
        return;
    }
    s.started = true;
    if matches!(s.kind, Kind::Buffer { .. }) {
        return;
    }
    let msg = with_keys(&s.msg, vec![("usage", usage_out(Some(usage)))]);
    s.stream_head(200, sse_headers());
    write(
        w,
        id,
        "message_start",
        json!({ "type": "message_start", "message": msg }),
    );
    let early = match w.sinks.get_mut(&id).map(|s| &mut s.kind) {
        Some(Kind::Sse { pre_start, .. }) => std::mem::take(pre_start),
        _ => Vec::new(),
    };
    let any = !early.is_empty();
    for text in early {
        if !raw(w, id, text) {
            return;
        }
    }
    if any {
        touch(w, id);
    }
    if !sink_done(w, id) {
        spawn_keepalive(w, id);
    }
}

pub fn text_delta(w: &mut World, id: u64, t: &str) {
    let Some(s) = w.sinks.get_mut(&id) else {
        return;
    };
    match &mut s.kind {
        Kind::Buffer { text, .. } => {
            if !t.is_empty() {
                text.push_str(t);
            }
        }
        Kind::Sse {
            text_open,
            any_block,
            index,
            ..
        } => {
            if t.is_empty() || s.done {
                return;
            }
            let idx = *index;
            if !*text_open {
                *text_open = true;
                *any_block = true;
                write(
                    w,
                    id,
                    "content_block_start",
                    json!({ "type": "content_block_start", "index": idx, "content_block": { "type": "text", "text": "" } }),
                );
            }
            write(
                w,
                id,
                "content_block_delta",
                json!({ "type": "content_block_delta", "index": idx, "delta": { "type": "text_delta", "text": t } }),
            );
        }
    }
}

fn flush_text(text: &mut String, blocks: &mut Vec<Value>) {
    if !text.is_empty() {
        blocks.push(json!({ "type": "text", "text": std::mem::take(text) }));
    }
}

fn close_text(w: &mut World, id: u64) {
    let Some(Kind::Sse {
        text_open, index, ..
    }) = w.sinks.get_mut(&id).map(|s| &mut s.kind)
    else {
        return;
    };
    if !*text_open {
        return;
    }
    let idx = *index;
    *text_open = false;
    *index += 1;
    write(
        w,
        id,
        "content_block_stop",
        json!({ "type": "content_block_stop", "index": idx }),
    );
}

/// `toolUse(block)`; `block` is `{type, id, name, input}` with absent keys omitted.
pub fn tool_use(w: &mut World, id: u64, block: &Value) {
    let Some(s) = w.sinks.get_mut(&id) else {
        return;
    };
    match &mut s.kind {
        Kind::Buffer { text, blocks } => {
            flush_text(text, blocks);
            blocks.push(block.clone());
        }
        Kind::Sse { .. } => {
            if s.done {
                return;
            }
            close_text(w, id);
            let idx = match w.sinks.get(&id).map(|s| &s.kind) {
                Some(Kind::Sse { index, .. }) => *index,
                _ => return,
            };
            let mut cb = Map::new();
            cb.insert("type".into(), Value::from("tool_use"));
            for k in ["id", "name"] {
                if let Some(v) = block.get(k) {
                    cb.insert(k.into(), v.clone());
                }
            }
            cb.insert("input".into(), json!({}));
            let input = block
                .get("input")
                .filter(|v| !v.is_null())
                .cloned()
                .unwrap_or(json!({}));
            write(
                w,
                id,
                "content_block_start",
                json!({ "type": "content_block_start", "index": idx, "content_block": cb }),
            );
            write(
                w,
                id,
                "content_block_delta",
                json!({ "type": "content_block_delta", "index": idx, "delta": { "type": "input_json_delta", "partial_json": input.to_string() } }),
            );
            write(
                w,
                id,
                "content_block_stop",
                json!({ "type": "content_block_stop", "index": idx }),
            );
            if let Some(Kind::Sse {
                index, any_block, ..
            }) = w.sinks.get_mut(&id).map(|s| &mut s.kind)
            {
                *index += 1;
                *any_block = true;
            }
        }
    }
}

pub fn finish(w: &mut World, id: u64, stop_reason: &Value, usage: &Usage) {
    let Some(s) = w.sinks.get_mut(&id) else {
        return;
    };
    if s.done {
        return;
    }
    match &mut s.kind {
        Kind::Buffer { text, blocks } => {
            flush_text(text, blocks);
            if blocks.is_empty() {
                blocks.push(json!({ "type": "text", "text": "(no text)" }));
            }
            let content = Value::Array(std::mem::take(blocks));
            s.done = true;
            let body = with_keys(
                &s.msg,
                vec![
                    ("content", content),
                    ("stop_reason", stop_reason.clone()),
                    ("usage", usage_out(Some(usage))),
                ],
            );
            s.full(Reply::json(200, &body, Vec::new()));
        }
        Kind::Sse { any_block, .. } => {
            if !*any_block {
                text_delta(w, id, "(no text)");
            }
            close_text(w, id);
            write(
                w,
                id,
                "message_delta",
                json!({ "type": "message_delta", "delta": { "stop_reason": stop_reason, "stop_sequence": null }, "usage": usage_out(Some(usage)) }),
            );
            write(w, id, "message_stop", json!({ "type": "message_stop" }));
            if let Some(s) = w.sinks.get_mut(&id) {
                s.done = true;
                clear_timer(s);
                if !s.ended {
                    s.end();
                }
            }
        }
    }
}

/// Before message_start an error is an HTTP status; after it, an `event: error` frame.
pub fn error(w: &mut World, id: u64, err: &SidecarError) {
    let Some(s) = w.sinks.get_mut(&id) else {
        return;
    };
    if s.done {
        return;
    }
    s.done = true;
    clear_timer(s);
    let buffered = matches!(s.kind, Kind::Buffer { .. });
    if buffered || !s.started {
        s.full(Reply::error(err));
        return;
    }
    if !s.ended && !s.destroyed {
        s.send(frame("error", &err.body()));
        s.end();
    }
}

/// The `close` event of the sink's response (`http::CloseGuard`): mark it destroyed and
/// hand back the registered hook.
pub fn closed(w: &mut World, id: u64) -> Option<CloseHook> {
    let s = w.sinks.get_mut(&id)?;
    s.destroyed = true;
    s.hook.take()
}
