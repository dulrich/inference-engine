//! `sinks.mjs` — the two response transports (`BufferSink`, `SseSink`), the message envelope
//! they share, and the one-shot writers. A sink is the only thing that writes to a client.
//!
//! `Res` stands in for Node's `ServerResponse`: the head goes to the waiting HTTP task as a
//! `Reply` (a whole body, or a stream fed through a channel), and `destroyed` is set by the
//! close event (`http::CloseGuard`).

use crate::errors::SidecarError;
use crate::js;
use crate::protocol::usage_out;
use crate::sidecar::{random_hex, with_world, World};
use bytes::Bytes;
use serde_json::{json, Map, Value};
use tokio::sync::{mpsc, oneshot};

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

pub struct Res {
    reply: Option<oneshot::Sender<Reply>>,
    body: Option<mpsc::UnboundedSender<Bytes>>,
    pub ended: bool,
    pub destroyed: bool,
    pub hook: Option<CloseHook>,
}

impl Res {
    pub fn new(reply: oneshot::Sender<Reply>) -> Self {
        Self {
            reply: Some(reply),
            body: None,
            ended: false,
            destroyed: false,
            hook: None,
        }
    }
    pub fn headers_sent(&self) -> bool {
        self.reply.is_none()
    }
    /// `writeHead(status, headers)` + `end(body)`.
    pub fn full(&mut self, status: u16, headers: Headers, body: Option<String>) {
        if let Some(tx) = self.reply.take() {
            let _ = tx.send(Reply::Full {
                status,
                headers,
                body,
            });
        }
        self.ended = true;
    }
    /// `writeHead(status, headers)` for a streamed body.
    pub fn stream_head(&mut self, status: u16, headers: Headers) {
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
    pub fn write(&mut self, text: &str) -> bool {
        if self.ended || self.destroyed {
            return false;
        }
        match &self.body {
            Some(b) => b.send(Bytes::from(text.to_string())).is_ok(),
            None => false,
        }
    }
    pub fn end(&mut self) {
        if self.reply.is_some() {
            self.full(200, Vec::new(), None);
        }
        self.ended = true;
        self.body = None;
    }
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

fn retry_after(err: &SidecarError) -> Option<String> {
    let v = err.extra.as_ref()?.get("retryAfter")?;
    if v.is_null() {
        return None;
    }
    Some(js::js_string(v))
}

fn error_headers(err: &SidecarError) -> Headers {
    let mut hs = json_headers();
    if let Some(r) = retry_after(err) {
        hs.push(("retry-after".into(), r));
    }
    hs
}

/// `sendJson(res, status, obj, headers)`.
pub fn send_json(w: &mut World, res: u64, status: u16, obj: &Value, extra: Headers) {
    if let Some(r) = w.res.get_mut(&res) {
        let mut hs = json_headers();
        for (k, v) in extra {
            hs.retain(|(hk, _)| hk != &k);
            hs.push((k, v));
        }
        r.full(status, hs, Some(js::stringify(obj)));
    }
}

/// `sendError(res, err)`.
pub fn send_error(w: &mut World, res: u64, err: &SidecarError) {
    let mut extra = Vec::new();
    if let Some(r) = retry_after(err) {
        extra.push(("retry-after".to_string(), r));
    }
    send_json(w, res, err.status, &err.body(), extra);
}

/// `wireModelId(personaName, requested)`: re-prefix the persona the router stripped.
pub fn wire_model_id(persona: &str, requested: Option<&Value>) -> String {
    let id = match requested {
        None | Some(Value::Null) => String::new(),
        Some(v) => js::js_string(v),
    };
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
        ping_ms: f64,
        index: f64,
        text_open: bool,
        any_block: bool,
        timer_gen: u64,
        pre_start: Vec<String>,
    },
}

pub struct Sink {
    pub res: u64,
    pub msg: Value,
    pub started: bool,
    pub done: bool,
    pub closed_by_client: bool,
    pub kind: Kind,
}

impl Sink {
    pub fn buffer(res: u64, model: &str) -> Self {
        Self {
            res,
            msg: message_skeleton(model),
            started: false,
            done: false,
            closed_by_client: false,
            kind: Kind::Buffer {
                text: String::new(),
                blocks: Vec::new(),
            },
        }
    }
    pub fn sse(res: u64, model: &str, ping_ms: f64) -> Self {
        Self {
            res,
            msg: message_skeleton(model),
            started: false,
            done: false,
            closed_by_client: false,
            kind: Kind::Sse {
                ping_ms,
                index: 0.0,
                text_open: false,
                any_block: false,
                timer_gen: 0,
                pre_start: Vec::new(),
            },
        }
    }
}

pub fn sink_done(w: &World, id: u64) -> bool {
    w.sinks.get(&id).is_none_or(|s| s.done)
}

fn frame(t: &str, obj: &Value) -> String {
    format!("event: {t}\ndata: {}\n\n", js::stringify(obj))
}

/// `SseSink.#raw(text)`.
fn raw(w: &mut World, id: u64, text: String) -> bool {
    let World { sinks, res, .. } = w;
    let Some(s) = sinks.get_mut(&id) else {
        return false;
    };
    let r = res.get_mut(&s.res);
    let gone = r.as_ref().is_none_or(|r| r.ended || r.destroyed);
    if s.done || gone {
        return false;
    }
    if !s.started {
        if let Kind::Sse { pre_start, .. } = &mut s.kind {
            pre_start.push(text);
        }
        return true;
    }
    if r.is_some_and(|r| r.write(&text)) {
        return true;
    }
    abort(w, id);
    false
}

fn write(w: &mut World, id: u64, t: &str, obj: Value) {
    if raw(w, id, frame(t, &obj)) {
        arm(w, id);
    }
}

fn arm(w: &mut World, id: u64) {
    let me = w.shared();
    let Some(s) = w.sinks.get_mut(&id) else {
        return;
    };
    let (done, started) = (s.done, s.started);
    let Kind::Sse {
        timer_gen, ping_ms, ..
    } = &mut s.kind
    else {
        return;
    };
    *timer_gen += 1;
    if done || !started {
        return;
    }
    let gen = *timer_gen;
    let ms = ping_ms.max(1.0) as u64;
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(ms)).await;
        with_world(&me, |w| {
            let live = w
                .sinks
                .get(&id)
                .is_some_and(|s| matches!(s.kind, Kind::Sse { timer_gen, .. } if timer_gen == gen));
            if live && raw(w, id, frame("ping", &json!({ "type": "ping" }))) {
                arm(w, id);
            }
        });
    });
}

fn clear_timer(s: &mut Sink) {
    if let Kind::Sse { timer_gen, .. } = &mut s.kind {
        *timer_gen += 1;
    }
}

pub fn abort(w: &mut World, id: u64) {
    if let Some(s) = w.sinks.get_mut(&id) {
        s.done = true;
        clear_timer(s);
    }
}

pub fn start(w: &mut World, id: u64, usage: &Value) {
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
    let res_id = s.res;
    let msg = with_keys(&s.msg, vec![("usage", usage_out(Some(usage)))]);
    if let Some(r) = w.res.get_mut(&res_id) {
        r.stream_head(200, sse_headers());
    }
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
        arm(w, id);
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
                    json!({ "type": "content_block_start", "index": js::num(idx), "content_block": { "type": "text", "text": "" } }),
                );
            }
            write(
                w,
                id,
                "content_block_delta",
                json!({ "type": "content_block_delta", "index": js::num(idx), "delta": { "type": "text_delta", "text": t } }),
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
    *index += 1.0;
    write(
        w,
        id,
        "content_block_stop",
        json!({ "type": "content_block_stop", "index": js::num(idx) }),
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
                json!({ "type": "content_block_start", "index": js::num(idx), "content_block": cb }),
            );
            write(
                w,
                id,
                "content_block_delta",
                json!({ "type": "content_block_delta", "index": js::num(idx), "delta": { "type": "input_json_delta", "partial_json": js::stringify(&input) } }),
            );
            write(
                w,
                id,
                "content_block_stop",
                json!({ "type": "content_block_stop", "index": js::num(idx) }),
            );
            if let Some(Kind::Sse {
                index, any_block, ..
            }) = w.sinks.get_mut(&id).map(|s| &mut s.kind)
            {
                *index += 1.0;
                *any_block = true;
            }
        }
    }
}

pub fn finish(w: &mut World, id: u64, stop_reason: &Value, usage: &Value) {
    let Some(s) = w.sinks.get_mut(&id) else {
        return;
    };
    if s.done {
        return;
    }
    let res_id = s.res;
    match &mut s.kind {
        Kind::Buffer { text, blocks } => {
            s.done = true;
            flush_text(text, blocks);
            if blocks.is_empty() {
                blocks.push(json!({ "type": "text", "text": "(no text)" }));
            }
            let body = with_keys(
                &s.msg,
                vec![
                    ("content", Value::Array(std::mem::take(blocks))),
                    ("stop_reason", stop_reason.clone()),
                    ("usage", usage_out(Some(usage))),
                ],
            );
            if let Some(r) = w.res.get_mut(&res_id) {
                r.full(200, json_headers(), Some(js::stringify(&body)));
            }
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
            }
            if let Some(r) = w.res.get_mut(&res_id) {
                if !r.ended {
                    r.end();
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
    let res_id = s.res;
    let buffered = matches!(s.kind, Kind::Buffer { .. });
    let started = s.started;
    let Some(r) = w.res.get_mut(&res_id) else {
        return;
    };
    if buffered || !started {
        r.full(
            err.status,
            error_headers(err),
            Some(js::stringify(&err.body())),
        );
        return;
    }
    if !r.ended && !r.destroyed {
        r.write(&frame("error", &err.body()));
        r.end();
    }
}
