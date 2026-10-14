//! Route handlers and the same-protocol relay. No protocol conversion exists here: a request
//! reaches only providers that speak its ingress protocol (Decision 2).

use crate::app::AppState;
use crate::auth;
use crate::config::{Kind, Provider};
use crate::local_leg::{HeaderMode, LocalLegContext};
use crate::routing::{self, Attempt, Candidate, Ingress, Leg, Resolved, Selectors};
use crate::telemetry::{Outcome, OutcomeKind, RequestStart, RequestTelemetry, Resolution};
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use futures_util::{Stream, StreamExt};
use serde_json::{json, Value};
use std::future::Future;
use std::io;
use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::{Duration, Instant, SystemTime};

pub type ByteStream = Pin<Box<dyn Stream<Item = Result<Bytes, io::Error>> + Send>>;

pub struct UpstreamResponse {
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: ByteStream,
}

#[derive(Clone, Debug)]
pub struct UpstreamFailure {
    pub timeout: bool,
    pub message: String,
}

pub type PendingUpstream =
    Pin<Box<dyn Future<Output = Result<UpstreamResponse, UpstreamFailure>> + Send>>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route {
    Messages,
    CountTokens,
    ChatCompletions,
    Systemone,
}

type St = State<Arc<AppState>>;

// ---------------------------------------------------------------- telemetry guard + tap

/// Owns the per-request telemetry handle; `Drop` before `finish` = client disconnect.
pub(crate) struct Guard {
    handle: Option<Box<dyn RequestTelemetry>>,
    started: Instant,
    headers_after: Option<Duration>,
    status: u16,
}

impl Guard {
    fn finish(&mut self, kind: OutcomeKind, status: u16, error: Option<(String, String)>) {
        if let Some(h) = self.handle.take() {
            let outcome = Outcome {
                kind,
                status,
                error,
                elapsed: self.started.elapsed(),
                upstream_headers_after: self.headers_after,
            };
            h.finalize(&outcome);
        }
    }

    fn resolved(&mut self, c: &Candidate, url: &str, attempt: usize) {
        if let Some(h) = self.handle.as_mut() {
            h.resolved(&Resolution {
                provider_name: &c.provider.name,
                provider_kind: c.provider.kind.token(),
                model: &c.model,
                upstream_url: url,
                attempt,
            });
        }
    }

    fn chunk(&mut self, b: &[u8]) {
        if let Some(h) = self.handle.as_mut() {
            h.upstream_chunk(b);
        }
    }
}

impl Drop for Guard {
    fn drop(&mut self) {
        let status = if self.headers_after.is_some() {
            self.status
        } else {
            499
        };
        self.finish(OutcomeKind::ClientDisconnect, status, None);
    }
}

static REQ_SEQ: AtomicU64 = AtomicU64::new(0);

fn request_id() -> String {
    use std::hash::{BuildHasher, Hasher};
    let seq = REQ_SEQ.fetch_add(1, Ordering::Relaxed);
    let mut a = std::collections::hash_map::RandomState::new().build_hasher();
    a.write_u64(seq);
    let x = a.finish();
    let mut b = std::collections::hash_map::RandomState::new().build_hasher();
    b.write_u64(x ^ seq.rotate_left(17));
    let y = b.finish();
    format!(
        "{:08x}-{:04x}-4{:03x}-{:04x}-{:012x}",
        x >> 32,
        (x >> 16) & 0xffff,
        x & 0xfff,
        ((y >> 48) & 0x3fff) | 0x8000,
        y & 0xffff_ffff_ffff
    )
}

fn start(st: &AppState, route: Route, headers: &HeaderMap, body: &Value) -> Guard {
    let id = request_id();
    let started = Instant::now();
    let handle = st.telemetry.request_start(&RequestStart {
        request_id: &id,
        route,
        requested_model: body.get("model").and_then(Value::as_str),
        headers,
        body: (route != Route::Systemone).then_some(body),
        streaming: is_streaming(body),
        started_at: started,
        started_wall: SystemTime::now(),
    });
    Guard {
        handle: Some(handle),
        started,
        headers_after: None,
        status: 0,
    }
}

struct Tap {
    inner: ByteStream,
    guard: Guard,
    ok: bool,
}

impl Stream for Tap {
    type Item = Result<Bytes, io::Error>;
    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = &mut *self;
        match this.inner.as_mut().poll_next(cx) {
            Poll::Ready(Some(Ok(b))) => {
                this.guard.chunk(&b);
                Poll::Ready(Some(Ok(b)))
            }
            Poll::Ready(Some(Err(e))) => {
                let kind = if e.kind() == io::ErrorKind::TimedOut {
                    OutcomeKind::Timeout
                } else {
                    OutcomeKind::UpstreamError
                };
                let status = this.guard.status;
                this.guard.finish(
                    kind,
                    status,
                    Some(("upstream_stream".into(), e.to_string())),
                );
                Poll::Ready(Some(Err(e)))
            }
            Poll::Ready(None) => {
                let (kind, status) = (
                    if this.ok {
                        OutcomeKind::Completed
                    } else {
                        OutcomeKind::UpstreamError
                    },
                    this.guard.status,
                );
                this.guard.finish(kind, status, None);
                Poll::Ready(None)
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

// ---------------------------------------------------------------- helpers

fn is_streaming(body: &Value) -> bool {
    body.get("stream") == Some(&Value::Bool(true))
}

fn json_response(status: u16, body: &Value) -> Response {
    let mut r = (
        StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY),
        body.to_string(),
    )
        .into_response();
    r.headers_mut().insert(
        "content-type",
        HeaderValue::from_static("application/json; charset=utf-8"),
    );
    r
}

fn error_body(message: &str) -> Value {
    json!({"error": {"message": message}})
}

fn refuse(guard: &mut Guard, status: u16, stage: &str, body: &Value) -> Response {
    let msg = body["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    guard.finish(OutcomeKind::Refused, status, Some((stage.into(), msg)));
    json_response(status, body)
}

fn hdr<'a>(h: &'a HeaderMap, name: &str) -> Option<&'a str> {
    h.get(name)
        .and_then(|v| v.to_str().ok())
        .filter(|v| !v.is_empty())
}

fn put(h: &mut HeaderMap, name: &'static str, value: &str) {
    if let Ok(v) = HeaderValue::from_str(value) {
        h.insert(name, v);
    }
}

const HOP_BY_HOP: [&str; 9] = [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
];

fn relay_headers(src: &HeaderMap) -> HeaderMap {
    let mut out = HeaderMap::new();
    for (k, v) in src {
        if !HOP_BY_HOP.contains(&k.as_str()) && k.as_str() != "host" {
            out.append(k.clone(), v.clone());
        }
    }
    out
}

fn io_err(e: reqwest::Error) -> io::Error {
    let kind = if e.is_timeout() {
        io::ErrorKind::TimedOut
    } else {
        io::ErrorKind::Other
    };
    io::Error::new(kind, e.to_string())
}

fn failure(e: reqwest::Error) -> UpstreamFailure {
    UpstreamFailure {
        timeout: e.is_timeout(),
        message: format!("Failed to reach upstream provider: {e}"),
    }
}

/// Parsed failure details as Node's `readUpstreamPayload`: JSON, else `{raw}`, empty → `{}`.
fn details(raw: &[u8]) -> Value {
    if raw.is_empty() {
        return json!({});
    }
    serde_json::from_slice(raw).unwrap_or_else(|_| json!({"raw": String::from_utf8_lossy(raw)}))
}

pub(crate) fn upstream_url(c: &Candidate, route: Route) -> Option<String> {
    let base = c.provider.baseurl.as_deref()?.trim_end_matches('/');
    let v1 = if base.ends_with("/v1") {
        base.to_string()
    } else {
        format!("{base}/v1")
    };
    Some(match (c.leg, route) {
        (Leg::OpenAiChat, _) => format!("{base}/chat/completions"),
        (_, Route::CountTokens) => format!("{v1}/messages/count_tokens"),
        (Leg::LocalMessages, _) => format!("{base}/messages"),
        (Leg::Anthropic, _) => format!("{base}/v1/messages"),
    })
}

fn attempt(p: &Arc<Provider>, stage: &'static str, status: u16, message: String) -> Attempt {
    Attempt {
        provider: p.clone(),
        stage,
        message,
        status: Some(status),
        details: None,
    }
}

/// Upstream header set, built from scratch (Node `buildAnthropicHeaders` `common.ts:46`,
/// `buildOpenAIHeaders` `:12`, `applyProviderCredentials` `handler.ts:7879`, 0001).
#[allow(clippy::result_large_err)] // internal, cold path; Attempt mirrors Node 1:1
pub(crate) fn upstream_headers(
    st: &AppState,
    c: &Candidate,
    route: Route,
    inbound: &HeaderMap,
) -> Result<HeaderMap, Attempt> {
    let p = &c.provider;
    let mut h = HeaderMap::new();
    put(&mut h, "content-type", "application/json");
    if c.leg == Leg::OpenAiChat {
        for name in ["openai-organization", "openai-project"] {
            if let Some(v) = hdr(inbound, name) {
                put(&mut h, name, v);
            }
        }
    } else {
        let version = hdr(inbound, "anthropic-version").or(hdr(inbound, "x-anthropic-version"));
        put(
            &mut h,
            "anthropic-version",
            version.unwrap_or(&st.settings.anthropic_version),
        );
        if let Some(beta) = hdr(inbound, "anthropic-beta") {
            put(&mut h, "anthropic-beta", beta);
        }
        if route == Route::Messages || p.apikey.is_none() {
            if let Some(ua) = hdr(inbound, "user-agent") {
                put(&mut h, "user-agent", ua);
            }
        }
    }
    match (&p.apikey, c.leg) {
        (Some(key), Leg::Anthropic) => {
            put(&mut h, "x-api-key", key);
            put(&mut h, "authorization", &format!("Bearer {key}"));
        }
        (Some(key), _) => put(&mut h, "authorization", &format!("Bearer {key}")),
        (None, Leg::Anthropic) if p.name == "anthropic" => {
            if let Some(key) = hdr(inbound, "x-api-key") {
                put(&mut h, "x-api-key", key);
                put(&mut h, "authorization", &format!("Bearer {key}"));
            } else if let Some(token) = hdr(inbound, "authorization").and_then(auth::bearer) {
                // 0001: an OAuth passthrough bearer is sent as `authorization` alone.
                put(&mut h, "authorization", &format!("Bearer {token}"));
            } else {
                // Pin 9: refuse clearly with 401, no upstream call (Node's 400 is not kept).
                return Err(attempt(p, NO_CALLER_CRED_STAGE, 401, NO_CALLER_CRED.into()));
            }
        }
        (None, _) => {
            return Err(attempt(
                p,
                "provider_config",
                503,
                format!("Provider {} has no configured credential; the caller's credential is never forwarded.", p.name),
            ))
        }
    }
    Ok(h)
}

const NO_CALLER_CRED_STAGE: &str = "caller_credential";
const NO_CALLER_CRED: &str = "No caller credential for the anthropic provider.";

fn local_ctx(c: &Candidate, route: Route, body: &Value) -> LocalLegContext {
    LocalLegContext {
        provider: c.provider.clone(),
        model: c.model.clone(),
        route,
        streaming: is_streaming(body),
    }
}

struct Prepared {
    url: String,
    headers: HeaderMap,
    body: Bytes,
    local: Option<LocalLegContext>,
}

#[allow(clippy::result_large_err)]
fn prepare(
    st: &AppState,
    c: &Candidate,
    route: Route,
    inbound: &HeaderMap,
    body: &Value,
) -> Result<Prepared, Attempt> {
    let url = upstream_url(c, route).ok_or_else(|| {
        attempt(
            &c.provider,
            "provider_config",
            503,
            format!("Provider {} has no baseurl.", c.provider.name),
        )
    })?;
    let headers = upstream_headers(st, c, route, inbound)?;
    let mut out = body.clone();
    out["model"] = Value::String(c.model.clone());
    let local = (c.leg == Leg::LocalMessages).then(|| local_ctx(c, route, body));
    if let Some(ctx) = &local {
        out = st.local_leg.transform_body(out, ctx);
    }
    Ok(Prepared {
        url,
        headers,
        body: Bytes::from(out.to_string()),
        local,
    })
}

async fn send_once(st: &AppState, p: &Prepared) -> Result<reqwest::Response, reqwest::Error> {
    st.client
        .post(&p.url)
        .headers(p.headers.clone())
        .body(p.body.clone())
        .send()
        .await
}

/// One retry on the same target after 150 ms when the request fails before any response
/// arrives (refused, reset, socket destroyed) but not on a timeout (Node
/// `upstream/client.ts:114-115`, catch at `:241`). No status code is ever retried.
async fn send(st: &AppState, p: &Prepared) -> Result<reqwest::Response, UpstreamFailure> {
    match send_once(st, p).await {
        Err(e) if !e.is_timeout() => {
            tokio::time::sleep(Duration::from_millis(150)).await;
            send_once(st, p).await.map_err(failure)
        }
        r => r.map_err(failure),
    }
}

fn selectors(h: &HeaderMap) -> Selectors<'_> {
    Selectors {
        providers: hdr(h, "x-target-providers"),
        provider: hdr(h, "x-target-provider"),
    }
}

fn into_upstream(resp: reqwest::Response) -> UpstreamResponse {
    let status = resp.status();
    let headers = relay_headers(resp.headers());
    UpstreamResponse {
        status,
        headers,
        body: Box::pin(resp.bytes_stream().map(|r| r.map_err(io_err))),
    }
}

/// Relay a live upstream response: status + headers + tapped (and local-wrapped) body.
fn relay_stream(
    st: &AppState,
    up: UpstreamResponse,
    mut guard: Guard,
    local: Option<&LocalLegContext>,
    extra: Option<&Candidate>,
) -> Response {
    guard.headers_after = Some(guard.started.elapsed());
    guard.status = up.status.as_u16();
    let ok = up.status.is_success();
    let mut body: ByteStream = Box::pin(Tap {
        inner: up.body,
        guard,
        ok,
    });
    if let Some(ctx) = local {
        body = st.local_leg.wrap_stream(body, ctx);
    }
    let mut r = Response::new(Body::from_stream(body));
    *r.status_mut() = up.status;
    *r.headers_mut() = up.headers;
    if let Some(c) = extra {
        put(
            r.headers_mut(),
            "x-gateway-target-provider",
            c.provider.kind.token(),
        );
        put(
            r.headers_mut(),
            "x-gateway-target-provider-name",
            &c.provider.name,
        );
    }
    r
}

// ---------------------------------------------------------------- handlers

fn parse_object(raw: &Bytes) -> Option<Value> {
    serde_json::from_slice::<Value>(raw)
        .ok()
        .filter(Value::is_object)
}

macro_rules! preamble {
    ($st:expr, $headers:expr, $raw:expr) => {{
        if let Err(m) = auth::check(&$st.settings.auth, &$headers) {
            return json_response(401, &error_body(&m));
        }
        match parse_object(&$raw) {
            Some(v) => v,
            None => return json_response(400, &error_body("Request body must be a JSON object.")),
        }
    }};
}

fn mismatch_body(model: &str, provider: &str, ingress: Ingress) -> Value {
    let proto = if ingress == Ingress::Chat {
        "OpenAI chat-completions"
    } else {
        "Anthropic Messages"
    };
    json!({"error": {
        "message": format!("Model \"{model}\" resolves to provider {provider}, which does not speak the {proto} protocol of this route; the gateway never converts protocols."),
        "type": "invalid_request_error",
        "code": "protocol_mismatch",
    }})
}

pub async fn messages(State(st): St, headers: HeaderMap, raw: Bytes) -> Response {
    let body = preamble!(st, headers, raw);
    fallthrough(&st, Route::Messages, Ingress::Messages, &headers, body).await
}

pub async fn chat_completions(State(st): St, headers: HeaderMap, raw: Bytes) -> Response {
    let body = preamble!(st, headers, raw);
    fallthrough(&st, Route::ChatCompletions, Ingress::Chat, &headers, body).await
}

async fn fallthrough(
    st: &Arc<AppState>,
    route: Route,
    ingress: Ingress,
    inbound: &HeaderMap,
    body: Value,
) -> Response {
    let mut guard = start(st, route, inbound, &body);
    let requested = body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let targets = match routing::resolve(&st.settings, ingress, &requested, selectors(inbound)) {
        Resolved::Mismatch { model, provider } => {
            return refuse(
                &mut guard,
                400,
                "protocol",
                &mismatch_body(&model, &provider, ingress),
            )
        }
        Resolved::BadRequest(msg) => {
            return refuse(&mut guard, 400, "target_selection", &error_body(&msg))
        }
        Resolved::Targets(t) => t,
    };
    let mut attempts: Vec<Attempt> = Vec::new();
    let mut upstream_called = false;
    for (i, t) in targets.iter().enumerate() {
        let last = i + 1 == targets.len();
        let c = match &t.plan {
            Ok(c) => c,
            Err(a) => {
                attempts.push(a.clone());
                continue;
            }
        };
        let prepared = match prepare(st, c, route, inbound, &body) {
            Ok(p) => p,
            Err(a) => {
                attempts.push(a);
                continue;
            }
        };
        guard.resolved(c, &prepared.url, i);
        upstream_called = true;
        if let Some(ctx) = prepared.local.as_ref().filter(|ctx| {
            route == Route::Messages
                && ctx.streaming
                && st.local_leg.header_mode(ctx) == HeaderMode::Early
        }) {
            return early(st, prepared.clone_parts(), guard, ctx.clone());
        }
        let resp = match send(st, &prepared).await {
            Ok(r) => r,
            Err(f) => {
                let kind = f.timeout;
                attempts.push(attempt(
                    &c.provider,
                    if kind {
                        "upstream_timeout"
                    } else {
                        "upstream_connect"
                    },
                    502,
                    f.message,
                ));
                continue;
            }
        };
        if resp.status().is_success() {
            return relay_stream(
                st,
                into_upstream(resp),
                guard,
                prepared.local.as_ref(),
                Some(c),
            );
        }
        let status = resp.status();
        let content_type = resp.headers().get("content-type").cloned();
        let raw = resp.bytes().await.unwrap_or_default();
        if last && ingress == Ingress::Messages && !raw.is_empty() {
            // 0003: final-attempt upstream error body, byte-verbatim.
            guard.chunk(&raw);
            guard.headers_after = Some(guard.started.elapsed());
            guard.finish(
                OutcomeKind::UpstreamError,
                status.as_u16(),
                Some((
                    "upstream_response".into(),
                    "Upstream request failed.".into(),
                )),
            );
            let mut r = Response::new(Body::from(raw));
            *r.status_mut() = status;
            if let Some(ct) = content_type {
                r.headers_mut().insert("content-type", ct);
            }
            return r;
        }
        attempts.push(Attempt {
            provider: c.provider.clone(),
            stage: "upstream_response",
            message: "Upstream request failed.".into(),
            status: Some(status.as_u16()),
            details: Some(details(&raw)),
        });
    }
    if let Some(a) = attempts
        .last()
        .filter(|a| !upstream_called && a.stage == NO_CALLER_CRED_STAGE)
    {
        let (status, msg) = (a.status.unwrap_or(401), a.message.clone());
        return refuse(&mut guard, status, NO_CALLER_CRED_STAGE, &error_body(&msg));
    }
    let (status, payload) = routing::envelope(&targets, &attempts);
    let last = attempts
        .last()
        .map(|a| (a.stage.to_string(), a.message.clone()));
    let kind = if upstream_called {
        OutcomeKind::UpstreamError
    } else {
        OutcomeKind::Refused
    };
    guard.finish(kind, status, last);
    json_response(status, &payload)
}

impl Prepared {
    fn clone_parts(&self) -> Prepared {
        Prepared {
            url: self.url.clone(),
            headers: self.headers.clone(),
            body: self.body.clone(),
            local: self.local.clone(),
        }
    }
}

/// Local-leg `HeaderMode::Early`: answer 200 + SSE headers now; the delegate owns the body.
fn early(
    st: &Arc<AppState>,
    prepared: Prepared,
    mut guard: Guard,
    ctx: LocalLegContext,
) -> Response {
    let st2 = st.clone();
    let pending: PendingUpstream = Box::pin(async move {
        match send(&st2, &prepared).await {
            Ok(resp) => {
                let up = into_upstream(resp);
                guard.headers_after = Some(guard.started.elapsed());
                guard.status = up.status.as_u16();
                let ok = up.status.is_success();
                Ok(UpstreamResponse {
                    status: up.status,
                    headers: up.headers,
                    body: Box::pin(Tap {
                        inner: up.body,
                        guard,
                        ok,
                    }),
                })
            }
            Err(f) => {
                let kind = if f.timeout {
                    OutcomeKind::Timeout
                } else {
                    OutcomeKind::UpstreamError
                };
                guard.headers_after = Some(guard.started.elapsed());
                guard.finish(
                    kind,
                    200,
                    Some(("upstream_connect".into(), f.message.clone())),
                );
                Err(f)
            }
        }
    });
    let body = st.local_leg.early_stream(pending, &ctx);
    let mut r = Response::new(Body::from_stream(body));
    put(
        r.headers_mut(),
        "content-type",
        "text/event-stream; charset=utf-8",
    );
    put(r.headers_mut(), "cache-control", "no-cache, no-transform");
    put(r.headers_mut(), "x-accel-buffering", "no");
    r
}

pub async fn count_tokens(State(st): St, headers: HeaderMap, raw: Bytes) -> Response {
    let body = preamble!(st, headers, raw);
    let mut guard = start(&st, Route::CountTokens, &headers, &body);
    let requested = body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let candidate = match routing::resolve(
        &st.settings,
        Ingress::CountTokens,
        &requested,
        selectors(&headers),
    ) {
        Resolved::Mismatch { model, provider } => {
            return refuse(
                &mut guard,
                400,
                "protocol",
                &mismatch_body(&model, &provider, Ingress::CountTokens),
            )
        }
        Resolved::BadRequest(msg) => {
            return refuse(&mut guard, 400, "target_selection", &error_body(&msg))
        }
        Resolved::Targets(t) => t
            .into_iter()
            .find_map(|t| t.plan.ok())
            .filter(|c| !c.model.is_empty()),
    };
    let Some(c) = candidate else {
        let shown = if requested.is_empty() {
            "(none)"
        } else {
            &requested
        };
        let b = json!({"error": {"message": format!("Model not found for /v1/messages/count_tokens: {shown}"), "type": "invalid_request_error", "code": "model_not_found"}});
        return refuse(&mut guard, 404, "model_resolution", &b);
    };
    let prepared = match prepare(&st, &c, Route::CountTokens, &headers, &body) {
        Ok(p) => p,
        Err(a) => {
            let status = a.status.unwrap_or(400);
            return refuse(&mut guard, status, a.stage, &error_body(&a.message));
        }
    };
    guard.resolved(&c, &prepared.url, 0);
    match send(&st, &prepared).await {
        Ok(resp) => relay_stream(
            &st,
            into_upstream(resp),
            guard,
            prepared.local.as_ref(),
            None,
        ),
        Err(f) => {
            let kind = if f.timeout {
                OutcomeKind::Timeout
            } else {
                OutcomeKind::UpstreamError
            };
            guard.finish(
                kind,
                502,
                Some(("upstream_connect".into(), f.message.clone())),
            );
            json_response(502, &error_body(&f.message))
        }
    }
}

/// 0013: strict allowlist, provider-credential-only bearer, verbatim relay.
pub async fn systemone(State(st): St, headers: HeaderMap, raw: Bytes) -> Response {
    if let Err(m) = auth::check(&st.settings.auth, &headers) {
        return json_response(401, &error_body(&m));
    }
    let Some(body) = parse_object(&raw) else {
        return json_response(
            400,
            &json!({"error": {"message": "Request body must be a JSON object.", "type": "invalid_request_error"}}),
        );
    };
    let mut guard = start(&st, Route::Systemone, &headers, &body);
    let requested = body.get("model").and_then(Value::as_str);
    let target = requested
        .map(str::trim)
        .and_then(|m| m.split_once('/'))
        .and_then(|(name, model)| {
            let p = st
                .settings
                .providers
                .iter()
                .find(|p| p.name == name && p.kind == Kind::Systemone)?;
            (!p.models.is_empty() && p.models.iter().any(|m| m == model))
                .then(|| (p.clone(), model.to_string()))
        });
    let Some((p, model)) = target else {
        let shown = requested.filter(|m| !m.is_empty()).unwrap_or("(none)");
        let b = json!({"error": {"message": format!("Model not found for /v1/systemone: {shown}"), "type": "invalid_request_error", "code": "model_not_found"}});
        return refuse(&mut guard, 404, "model_resolution", &b);
    };
    let (Some(base), Some(key)) = (p.baseurl.as_deref(), p.apikey.as_deref()) else {
        let b = json!({"error": {"message": format!("Provider {} is not configured with a baseurl and credential.", p.name), "type": "api_error", "code": "provider_not_configured"}});
        return refuse(&mut guard, 503, "provider_config", &b);
    };
    let url = format!("{}/systemone", base.trim_end_matches('/'));
    let mut h = HeaderMap::new();
    put(&mut h, "content-type", "application/json");
    put(&mut h, "authorization", &format!("Bearer {key}"));
    let mut out = body.clone();
    out["model"] = Value::String(model.clone());
    let c = Candidate {
        provider: p.clone(),
        model,
        leg: Leg::OpenAiChat,
    };
    guard.resolved(&c, &url, 0);
    let prepared = Prepared {
        url,
        headers: h,
        body: Bytes::from(out.to_string()),
        local: None,
    };
    match send(&st, &prepared).await {
        Ok(resp) => relay_stream(&st, into_upstream(resp), guard, None, None),
        Err(f) => {
            let kind = if f.timeout {
                OutcomeKind::Timeout
            } else {
                OutcomeKind::UpstreamError
            };
            guard.finish(
                kind,
                502,
                Some(("upstream_connect".into(), f.message.clone())),
            );
            json_response(502, &error_body(&f.message))
        }
    }
}

/// Node `routes.ts:69` (`buildGatewayModelListEntries` `:340`, format `:271`).
pub async fn models(State(st): St, headers: HeaderMap, uri: Uri) -> Response {
    if let Err(m) = auth::check(&st.settings.auth, &headers) {
        return json_response(401, &error_body(&m));
    }
    let query_fmt = uri.query().unwrap_or_default().split('&').find_map(|kv| {
        let (k, v) = kv.split_once('=')?;
        (k == "format" || k == "protocol").then(|| v.to_ascii_lowercase())
    });
    let fmt = query_fmt
        .or_else(|| hdr(&headers, "x-gateway-model-list-format").map(str::to_ascii_lowercase))
        .filter(|f| matches!(f.as_str(), "anthropic" | "claude" | "openai"));
    let anthropic = match fmt.as_deref() {
        Some("openai") => false,
        Some(_) => true,
        None => {
            hdr(&headers, "anthropic-version").is_some()
                || hdr(&headers, "anthropic-beta").is_some()
        }
    };
    let mut ids: Vec<(String, String, String)> = Vec::new();
    for p in &st.settings.providers {
        for m in p.models.iter().filter(|m| !m.is_empty()) {
            let id = format!("{}/{m}", p.name);
            if !ids.iter().any(|(i, _, _)| *i == id) {
                ids.push((id, m.clone(), p.name.clone()));
            }
        }
    }
    let body = if anthropic {
        json!({
            "data": ids.iter().map(|(id, m, _)| json!({"created_at": "1970-01-01T00:00:00Z", "display_name": m, "id": id, "type": "model"})).collect::<Vec<_>>(),
            "first_id": ids.first().map(|i| i.0.clone()),
            "has_more": false,
            "last_id": ids.last().map(|i| i.0.clone()),
        })
    } else {
        json!({"object": "list", "data": ids.iter().map(|(id, _, owner)| json!({"id": id, "object": "model", "created": 0, "owned_by": owner})).collect::<Vec<_>>()})
    };
    json_response(200, &body)
}

/// Unauthenticated liveness, Node `index.ts:178`.
pub async fn health() -> Response {
    json_response(200, &json!({"status": "ok", "timestamp": iso_now()}))
}

fn iso_now() -> String {
    let d = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = d.as_secs() as i64;
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60,
        d.subsec_millis()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_timestamp_shape() {
        let t = iso_now();
        assert_eq!(t.len(), 24);
        assert!(t.ends_with('Z') && t.as_bytes()[10] == b'T');
    }

    #[test]
    fn request_ids_are_distinct() {
        assert_ne!(request_id(), request_id());
        assert_eq!(request_id().len(), 36);
    }
}
