//! The HTTP surface: auth (before any body byte is read), the closed route
//! table, the 32 MiB body cap, and bind-aware `/healthz`.
//!
//! Each request runs as its own task, so it keeps running after the client goes;
//! the axum handler only waits for the head (`sinks::Reply`). A `CloseGuard` travels with
//! the response — dropped with the handler future, or with the streamed body — and fires
//! the sink's close hook.

use crate::config::{BODY_LIMIT, VERSION};
use crate::errors::{as_sidecar_error, SidecarError};
use crate::logging::redact_headers;
use crate::messages::{admit, on_joiner_close, on_owner_close, run, Admit};
use crate::protocol::{count_tokens_estimate, presented_key, safe_equal};
use crate::request::Request as AnthropicRequest;
use crate::sidecar::{err, lock, with_world, Shared, World};
use crate::sinks::{self, json_headers, CloseHook, Reply};
use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::HeaderMap;
use axum::response::Response;
use axum::Router;
use bytes::Bytes;
use serde_json::{json, Map, Value};
use std::sync::{Mutex, Weak};
use tokio::sync::oneshot;

pub fn router(world: Shared) -> Router {
    Router::new().fallback(handler).with_state(world)
}

/// The `/healthz` payload.
pub fn health_body(w: &World) -> Value {
    let mut personas = Map::new();
    let mut degraded = false;
    for p in &w.personas {
        personas.insert(
            p.name.clone(),
            json!({ "alive": p.child_alive, "degraded": p.degraded }),
        );
        degraded |= p.degraded;
    }
    let missing: Vec<&String> = w
        .binds
        .expected
        .iter()
        .filter(|b| !w.binds.bound.contains(b))
        .collect();
    if !missing.is_empty() {
        degraded = true;
    }
    json!({
        "status": if degraded { "degraded" } else { "ok" },
        "version": VERSION,
        "personas": personas,
        "binds": { "expected": w.binds.expected, "bound": w.binds.bound, "missing": missing },
        "degraded": degraded,
    })
}

struct CloseGuard {
    world: Weak<Mutex<World>>,
    sink: u64,
}

impl Drop for CloseGuard {
    fn drop(&mut self) {
        let (w, id) = (self.world.clone(), self.sink);
        // never lock here: the drop can happen inside a critical section
        tokio::spawn(async move {
            with_world(&w, |w| sink_closed(w, id));
        });
    }
}

/// The response's close event.
fn sink_closed(w: &mut World, id: u64) {
    match sinks::closed(w, id) {
        Some(CloseHook::Owner {
            sid,
            token,
            sink,
            persona,
        }) => on_owner_close(w, sid, token, sink, persona),
        Some(CloseHook::Joiner {
            sid,
            joiner,
            sink,
            persona,
        }) => on_joiner_close(w, sid, joiner, sink, persona),
        None => {}
    }
}

fn build(status: u16, headers: Vec<(String, String)>, body: Body) -> Response {
    let mut b = Response::builder().status(status);
    for (k, v) in headers {
        b = b.header(k, v);
    }
    b.body(body)
        .unwrap_or_else(|_| Response::new(Body::empty()))
}

/// A reply as a response; a streamed body carries the close guard until it ends.
fn respond(reply: Reply, guard: Option<CloseGuard>) -> Response {
    match reply {
        Reply::Full {
            status,
            headers,
            body,
        } => build(status, headers, body.map_or_else(Body::empty, Body::from)),
        Reply::Stream {
            status,
            headers,
            mut rx,
        } => {
            let mut guard = guard;
            let stream = futures_util::stream::poll_fn(move |cx| {
                let _keep = &guard;
                match rx.poll_recv(cx) {
                    std::task::Poll::Ready(Some(b)) => {
                        std::task::Poll::Ready(Some(Ok::<Bytes, std::io::Error>(b)))
                    }
                    std::task::Poll::Ready(None) => {
                        guard.take();
                        std::task::Poll::Ready(None)
                    }
                    std::task::Poll::Pending => std::task::Poll::Pending,
                }
            });
            build(status, headers, Body::from_stream(stream))
        }
    }
}

/// What a request resolved to: a whole response, or a messages request whose head the
/// spawned driver will send to `rx` (its sink is `sink`).
enum Step {
    Done(Reply),
    Wait {
        sink: u64,
        rx: oneshot::Receiver<Reply>,
    },
}

/// The one `http-error` row of a request that failed before a sink took it over.
fn fail(me: &Shared, path: &str, e: &SidecarError) -> Reply {
    lock(me).log(
        "http-error",
        json!({ "path": path, "status": e.status, "type": e.anthropic_type, "message": e.message }),
    );
    Reply::error(e)
}

async fn handler(State(world): State<Shared>, req: Request) -> Response {
    let (parts, body) = req.into_parts();
    let path = parts.uri.path().to_string();
    let method = parts.method.as_str().to_string();
    let headers = parts.headers;
    let target = match front(&world, &method, &path, &headers) {
        Ok(t) => t,
        Err(reply) => return respond(reply, None),
    };
    // The body read and everything after it run as their own task: the request keeps
    // running (and writes its row) when the client goes.
    let me = world.clone();
    let task = tokio::spawn(async move { post(&me, target, &path, headers, body).await });
    match task.await {
        Ok(Step::Done(reply)) => respond(reply, None),
        Ok(Step::Wait { sink, rx }) => {
            let guard = CloseGuard {
                world: std::sync::Arc::downgrade(&world),
                sink,
            };
            match rx.await {
                Ok(reply) => respond(reply, Some(guard)),
                Err(_) => build(500, json_headers(), Body::empty()),
            }
        }
        Err(_) => build(500, json_headers(), Body::empty()),
    }
}

/// The routes answered from the head alone: auth, `/healthz`, the 404s and `hello`.
/// `Ok` is a POST route to read the body of: `(persona index, persona name, rest)`.
fn front(
    me: &Shared,
    method: &str,
    path: &str,
    headers: &HeaderMap,
) -> Result<(usize, String, String), Reply> {
    let mut w = lock(me);
    let w = &mut *w;
    let presented = presented_key(headers).unwrap_or_default();
    if !safe_equal(&presented, &w.cfg.key) {
        w.log(
            "auth-failed",
            json!({ "path": path, "method": method, "headers": redact_headers(headers) }),
        );
        return Err(Reply::error(&err(
            401,
            "authentication_error",
            "missing or invalid sidecar key",
        )));
    }
    if path == "/healthz" {
        let b = health_body(w);
        let st = if b.get("degraded") == Some(&Value::Bool(true)) {
            503
        } else {
            200
        };
        return Err(Reply::json(st, &b, Vec::new()));
    }
    let seg: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    let dotted = has_dot_segment(path);
    let pi = seg
        .first()
        .and_then(|s| w.persona_index(s))
        .filter(|_| !dotted);
    let rest = format!(
        "/{}",
        seg.iter().skip(1).copied().collect::<Vec<_>>().join("/")
    );
    let Some(pi) = pi else {
        return Err(Reply::error(&err(
            404,
            "not_found_error",
            format!("unknown persona in path {path}"),
        )));
    };
    let name = w.personas[pi].name.clone();
    if rest == "/api/hello" && (method == "GET" || method == "HEAD") {
        w.log("hello", json!({ "persona": name, "method": method }));
        let b = (method != "HEAD").then(|| "{}".to_string());
        return Err(Reply::Full {
            status: 200,
            headers: json_headers(),
            body: b,
        });
    }
    if method != "POST" || (rest != "/v1/messages" && rest != "/v1/messages/count_tokens") {
        return Err(Reply::error(&err(
            404,
            "not_found_error",
            format!("unsupported path {path}"),
        )));
    }
    Ok((pi, name, rest))
}

/// A `.`/`..` segment, plain or percent-encoded: such a path is a 404 (never normalized).
pub fn has_dot_segment(path: &str) -> bool {
    path.split('/').any(|s| {
        matches!(
            s.to_ascii_lowercase().as_str(),
            "." | ".." | "%2e" | "%2e%2e" | ".%2e" | "%2e."
        )
    })
}

/// The axum body-limit rejection, found by type in the error's source chain.
fn is_length_limit(e: &axum::Error) -> bool {
    let mut cur: Option<&(dyn std::error::Error + 'static)> = Some(e);
    while let Some(x) = cur {
        if x.is::<http_body_util::LengthLimitError>() {
            return true;
        }
        cur = x.source();
    }
    false
}

async fn post(
    me: &Shared,
    (pi, name, rest): (usize, String, String),
    path: &str,
    headers: HeaderMap,
    body: Body,
) -> Step {
    let bytes = match axum::body::to_bytes(body, BODY_LIMIT).await {
        Ok(b) => b,
        Err(e) if is_length_limit(&e) => {
            let e = err(413, "invalid_request_error", "request body exceeds 32 MiB");
            let mut reply = fail(me, path, &e);
            if let Reply::Full { headers, .. } = &mut reply {
                headers.push(("connection".into(), "close".into()));
            }
            return Step::Done(reply);
        }
        Err(e) => return Step::Done(fail(me, path, &as_sidecar_error(&e.to_string()))),
    };
    let raw = String::from_utf8_lossy(&bytes);
    let Ok(body) = serde_json::from_str::<Value>(&raw) else {
        return Step::Done(fail(
            me,
            path,
            &err(
                400,
                "invalid_request_error",
                "request body is not valid JSON",
            ),
        ));
    };
    if rest == "/v1/messages/count_tokens" {
        let req = match AnthropicRequest::parse(body) {
            Ok(r) => r,
            Err(e) => return Step::Done(fail(me, path, &e)),
        };
        let n = count_tokens_estimate(&req, &req.messages);
        lock(me).log(
            "count_tokens",
            json!({ "persona": name, "input_tokens": n }),
        );
        return Step::Done(Reply::json(200, &json!({ "input_tokens": n }), Vec::new()));
    }
    let admitted = match admit(me, pi, body, headers) {
        Ok(Admit::Local(reply)) => return Step::Done(reply),
        Ok(Admit::Run(a)) => a,
        Err(e) => return Step::Done(fail(me, path, &e)),
    };
    let sink = lock(me).id();
    let (tx, rx) = oneshot::channel();
    let me = me.clone();
    let path = path.to_string();
    tokio::spawn(async move {
        let mut reply = Some(tx);
        if let Err(e) = run(&me, *admitted, sink, &mut reply).await {
            let r = fail(&me, &path, &e);
            if let Some(tx) = reply {
                let _ = tx.send(r);
            }
        }
    });
    Step::Wait { sink, rx }
}
