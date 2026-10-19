//! `http.mjs` — the HTTP surface: auth (before any body byte is read), the closed route
//! table, the 32 MiB body cap, and bind-aware `/healthz`.
//!
//! Each request runs as its own task (Node's handler keeps running after the client goes);
//! the axum handler only waits for the head (`sinks::Reply`). A `CloseGuard` travels with
//! the response — dropped with the handler future, or with the streamed body — and fires
//! the `res.on("close")` hook, as Node's `close` event does.

use crate::config::{BODY_LIMIT, VERSION};
use crate::errors::{as_sidecar_error, SidecarError};
use crate::js;
use crate::logging::redact_headers;
use crate::messages::{handle_messages, on_joiner_close, on_owner_close};
use crate::protocol::{count_tokens_estimate, presented_key, safe_equal};
use crate::sidecar::{err, lock, with_world, Shared, World};
use crate::sinks::{json_headers, send_error, send_json, CloseHook, Reply, Res};
use axum::body::Body;
use axum::extract::{Request, State};
use axum::response::Response;
use axum::Router;
use bytes::Bytes;
use serde_json::{json, Map, Value};
use std::sync::{Mutex, Weak};

pub fn router(world: Shared) -> Router {
    Router::new().fallback(handler).with_state(world)
}

/// `healthBody(ctx)`.
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

/// Node's `req.headers`: lower-cased names, arrival order, duplicates joined with `, `
/// (`cookie` with `; `, `set-cookie` as an array, singletons keep the first value).
pub fn node_headers(h: &axum::http::HeaderMap) -> Map<String, Value> {
    const SINGLE: &[&str] = &[
        "age",
        "authorization",
        "content-length",
        "content-type",
        "etag",
        "expires",
        "from",
        "host",
        "if-modified-since",
        "if-unmodified-since",
        "last-modified",
        "location",
        "max-forwards",
        "proxy-authorization",
        "referer",
        "retry-after",
        "server",
        "user-agent",
    ];
    let mut m = Map::new();
    for (k, v) in h {
        let name = k.as_str().to_string();
        let val = String::from_utf8_lossy(v.as_bytes()).to_string();
        if name == "set-cookie" {
            let e = m.entry(name).or_insert_with(|| Value::Array(Vec::new()));
            if let Value::Array(a) = e {
                a.push(Value::from(val));
            }
            continue;
        }
        match m.get_mut(&name) {
            None => {
                m.insert(name, Value::from(val));
            }
            Some(_) if SINGLE.contains(&name.as_str()) => {}
            Some(Value::String(prev)) => {
                let sep = if name == "cookie" { "; " } else { ", " };
                prev.push_str(sep);
                prev.push_str(&val);
            }
            Some(_) => {}
        }
    }
    m
}

/// `new URL(req.url, base).pathname` for the paths this server sees: dot segments removed.
fn pathname(raw: &str) -> String {
    let p = raw.split(['?', '#']).next().unwrap_or("");
    let p = p.replace('\\', "/");
    let mut out: Vec<&str> = Vec::new();
    let segs: Vec<&str> = p.split('/').skip(1).collect();
    let n = segs.len();
    for (i, s) in segs.iter().enumerate() {
        let last = i + 1 == n;
        match *s {
            "." | "%2e" | "%2E" => {
                if last {
                    out.push("");
                }
            }
            ".." | ".%2e" | "%2e." | "%2e%2e" | ".%2E" | "%2E." | "%2E%2E" => {
                out.pop();
                if last {
                    out.push("");
                }
            }
            s => out.push(s),
        }
    }
    format!("/{}", out.join("/"))
}

struct CloseGuard {
    world: Weak<Mutex<World>>,
    res: u64,
}

impl Drop for CloseGuard {
    fn drop(&mut self) {
        let (w, id) = (self.world.clone(), self.res);
        // never lock here: the drop can happen inside a critical section
        tokio::spawn(async move {
            with_world(&w, |w| res_closed(w, id));
        });
    }
}

/// Node's `res` `close` event.
fn res_closed(w: &mut World, id: u64) {
    let Some(r) = w.res.get_mut(&id) else {
        return;
    };
    r.destroyed = true;
    let hook = r.hook.take();
    w.res.remove(&id);
    match hook {
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

async fn handler(State(world): State<Shared>, req: Request) -> Response {
    let (tx, rx) = tokio::sync::oneshot::channel::<Reply>();
    let id = {
        let mut w = lock(&world);
        let id = w.id();
        w.res.insert(id, Res::new(tx));
        id
    };
    let guard = CloseGuard {
        world: std::sync::Arc::downgrade(&world),
        res: id,
    };
    let me = world.clone();
    tokio::spawn(async move {
        serve_request(&me, id, req).await;
    });
    match rx.await {
        Ok(Reply::Full {
            status,
            headers,
            body,
        }) => build(status, headers, body.map_or_else(Body::empty, Body::from)),
        Ok(Reply::Stream {
            status,
            headers,
            mut rx,
        }) => {
            let mut guard = Some(guard);
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
        Err(_) => build(500, json_headers(), Body::empty()),
    }
}

async fn serve_request(me: &Shared, id: u64, req: Request) {
    let method = req.method().as_str().to_string();
    let path = pathname(req.uri().path_and_query().map_or("/", |p| p.as_str()));
    let headers = node_headers(req.headers());
    let r = route(me, id, &method, &path, headers, req.into_body()).await;
    if let Err(e) = r {
        let mut w = lock(me);
        let w = &mut *w;
        w.log(
            "http-error",
            json!({ "path": path, "status": e.status, "type": e.anthropic_type, "message": e.message }),
        );
        match w.res.get_mut(&id) {
            Some(r) if !r.headers_sent() => send_error(w, id, &e),
            Some(r) if !r.ended => r.end(),
            _ => {}
        }
    }
}

async fn route(
    me: &Shared,
    id: u64,
    method: &str,
    path: &str,
    headers: Map<String, Value>,
    body: Body,
) -> Result<(), SidecarError> {
    let persona = {
        let mut w = lock(me);
        let w = &mut *w;
        let presented = presented_key(&headers).unwrap_or_default();
        if !safe_equal(&presented, &w.cfg.key) {
            w.log(
                "auth-failed",
                json!({ "path": path, "method": method, "headers": redact_headers(&headers) }),
            );
            send_error(
                w,
                id,
                &err(
                    401,
                    "authentication_error",
                    "missing or invalid sidecar key",
                ),
            );
            return Ok(());
        }
        if path == "/healthz" {
            let b = health_body(w);
            let st = if js::truthy(b.get("degraded")) {
                503
            } else {
                200
            };
            send_json(w, id, st, &b, Vec::new());
            return Ok(());
        }
        let seg: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        let pi = seg.first().and_then(|s| w.persona_index(s));
        let rest = format!(
            "/{}",
            seg.iter().skip(1).copied().collect::<Vec<_>>().join("/")
        );
        let Some(pi) = pi else {
            send_error(
                w,
                id,
                &err(
                    404,
                    "not_found_error",
                    format!("unknown persona in path {path}"),
                ),
            );
            return Ok(());
        };
        let name = w.personas[pi].name.clone();
        if rest == "/api/hello" && (method == "GET" || method == "HEAD") {
            w.log("hello", json!({ "persona": name, "method": method }));
            if let Some(r) = w.res.get_mut(&id) {
                let b = (method != "HEAD").then(|| "{}".to_string());
                r.full(200, json_headers(), b);
            }
            return Ok(());
        }
        if method != "POST" || (rest != "/v1/messages" && rest != "/v1/messages/count_tokens") {
            send_error(
                w,
                id,
                &err(404, "not_found_error", format!("unsupported path {path}")),
            );
            return Ok(());
        }
        (pi, name, rest)
    };
    let (pi, name, rest) = persona;
    let bytes = match axum::body::to_bytes(body, BODY_LIMIT).await {
        Ok(b) => b,
        Err(e) => {
            let too_big = std::error::Error::source(&e)
                .map(|s| s.to_string())
                .unwrap_or_default()
                .contains("length limit")
                || e.to_string().contains("length limit");
            if too_big {
                let mut w = lock(me);
                if let Some(r) = w.res.get_mut(&id) {
                    if !r.headers_sent() {
                        r.full(
                            413,
                            vec![
                                ("content-type".into(), "application/json".into()),
                                ("connection".into(), "close".into()),
                            ],
                            Some(js::stringify(
                                &err(413, "invalid_request_error", "request body exceeds 32 MiB")
                                    .body(),
                            )),
                        );
                    }
                }
                drop(w);
                return Err(err(
                    413,
                    "invalid_request_error",
                    "request body exceeds 32 MiB",
                ));
            }
            return Err(as_sidecar_error(&e.to_string()));
        }
    };
    let raw = String::from_utf8_lossy(&bytes);
    let body: Value = serde_json::from_str(&raw).map_err(|_| {
        err(
            400,
            "invalid_request_error",
            "request body is not valid JSON",
        )
    })?;
    if rest == "/v1/messages/count_tokens" {
        let n = count_tokens_estimate(&body);
        let mut w = lock(me);
        let w = &mut *w;
        w.log(
            "count_tokens",
            json!({ "persona": name, "input_tokens": js::num(n) }),
        );
        send_json(
            w,
            id,
            200,
            &json!({ "input_tokens": js::num(n) }),
            Vec::new(),
        );
        return Ok(());
    }
    handle_messages(me, pi, body, headers, id).await
}
