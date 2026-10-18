//! Route handlers and the same-protocol relay. No protocol conversion exists here: a request
//! reaches only providers that speak its ingress protocol (Decision 2).

use crate::app::AppState;
use crate::auth;
use crate::config::Kind;
use crate::local_leg::{self, LocalLegContext};
use crate::routing::{self, Attempt, Candidate, Ingress, Leg, Resolved, Selectors, Target};
use crate::telemetry::{Outcome, OutcomeKind, RequestStart, RequestTelemetry, Resolution};
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use futures_util::Stream;
use serde_json::{json, Value};
use std::io;
use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::task::{Context, Poll};
use std::time::{Duration, Instant, SystemTime};

mod upstream;

use upstream::{
    attempt, hdr, into_upstream, is_streaming, prepare, send, Prepared, NO_CALLER_CRED_STAGE,
};
pub use upstream::{UpstreamFailure, UpstreamResponse};

pub type ByteStream = Pin<Box<dyn Stream<Item = Result<Bytes, io::Error>> + Send>>;

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
static REQ_PREFIX: OnceLock<u64> = OnceLock::new();

/// `<process-random 16 hex>-<sequence 16 hex>`; the telemetry `reqId` schema is a free string.
fn request_id() -> String {
    let prefix = *REQ_PREFIX.get_or_init(|| {
        use std::hash::{BuildHasher, Hasher};
        let mut h = std::collections::hash_map::RandomState::new().build_hasher();
        h.write_u32(std::process::id());
        h.finish()
    });
    let seq = REQ_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("{prefix:016x}-{seq:016x}")
}

fn start(st: &AppState, route: Route, body: &Value) -> Guard {
    let id = request_id();
    let started = Instant::now();
    let handle = st.telemetry.request_start(&RequestStart {
        request_id: &id,
        route,
        requested_model: body.get("model").and_then(Value::as_str),
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

fn typed_error(message: &str, kind: &str, code: Option<&str>) -> Value {
    let mut e = json!({"message": message, "type": kind});
    if let Some(code) = code {
        e["code"] = json!(code);
    }
    json!({ "error": e })
}

fn refuse(guard: &mut Guard, status: u16, stage: &str, body: &Value) -> Response {
    let msg = body["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    guard.finish(OutcomeKind::Refused, status, Some((stage.into(), msg)));
    json_response(status, body)
}

fn selectors(h: &HeaderMap) -> Selectors<'_> {
    Selectors {
        providers: hdr(h, "x-target-providers"),
        provider: hdr(h, "x-target-provider"),
    }
}

/// Parsed failure details as Node's `readUpstreamPayload`: JSON, else `{raw}`, empty → `{}`.
fn details(raw: &[u8]) -> Value {
    if raw.is_empty() {
        return json!({});
    }
    serde_json::from_slice(raw).unwrap_or_else(|_| json!({"raw": String::from_utf8_lossy(raw)}))
}

/// Gateway-set informational response header. A configured provider name that is not a valid
/// header value only omits this header; the relayed response is otherwise unaffected.
fn tag(r: &mut Response, name: &'static str, value: &str) {
    if let Ok(v) = HeaderValue::from_str(value) {
        r.headers_mut().insert(name, v);
    }
}

/// Relay a live upstream response: status + headers + tapped (and local-wrapped) body.
fn relay_stream(
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
        body = local_leg::wrap_stream(body, ctx);
    }
    let mut r = Response::new(Body::from_stream(body));
    *r.status_mut() = up.status;
    *r.headers_mut() = up.headers;
    if let Some(c) = extra {
        tag(&mut r, "x-gateway-target-provider", c.provider.kind.token());
        tag(&mut r, "x-gateway-target-provider-name", &c.provider.name);
    }
    r
}

/// The shared send step: record the resolution, send (with the connect-only retry), and on
/// failure classify it once into (telemetry kind, stage, message).
async fn dispatch(
    st: &AppState,
    guard: &mut Guard,
    c: &Candidate,
    attempt_no: usize,
    prepared: &Prepared,
) -> Result<reqwest::Response, (OutcomeKind, &'static str, String)> {
    guard.resolved(c, &prepared.url, attempt_no);
    send(st, prepared).await.map_err(|f| {
        let (kind, stage) = f.classify();
        (kind, stage, f.message)
    })
}

/// Single-target relay (count_tokens, systemone): prepare, send, relay status + body verbatim.
async fn relay_one(
    st: &AppState,
    mut guard: Guard,
    c: &Candidate,
    route: Route,
    inbound: &HeaderMap,
    body: &Value,
) -> Response {
    let prepared = match prepare(st, c, route, inbound, body) {
        Ok(p) => p,
        Err(a) => {
            let status = a.status.unwrap_or(400);
            return refuse(&mut guard, status, a.stage, &error_body(&a.message));
        }
    };
    match dispatch(st, &mut guard, c, 0, &prepared).await {
        Ok(resp) => relay_stream(into_upstream(resp), guard, prepared.local.as_ref(), None),
        Err((kind, stage, message)) => {
            guard.finish(kind, 502, Some((stage.into(), message.clone())));
            json_response(502, &typed_error(&message, "api_error", None))
        }
    }
}

// ---------------------------------------------------------------- handlers

/// Client auth, then the body as a JSON object; the `Err` is the finished 401/400 response.
#[allow(clippy::result_large_err)]
fn preamble(
    st: &AppState,
    headers: &HeaderMap,
    raw: &Bytes,
    route: Route,
) -> Result<Value, Response> {
    if let Err(m) = auth::check(&st.settings.auth, headers) {
        return Err(json_response(401, &error_body(&m)));
    }
    let parsed = serde_json::from_slice::<Value>(raw)
        .ok()
        .filter(Value::is_object);
    parsed.ok_or_else(|| {
        let msg = "Request body must be a JSON object.";
        let body = if route == Route::Systemone {
            typed_error(msg, "invalid_request_error", None)
        } else {
            error_body(msg)
        };
        json_response(400, &body)
    })
}

fn mismatch_body(model: &str, provider: &str, ingress: Ingress) -> Value {
    let proto = if ingress == Ingress::Chat {
        "OpenAI chat-completions"
    } else {
        "Anthropic Messages"
    };
    let msg = format!("Model \"{model}\" resolves to provider {provider}, which does not speak the {proto} protocol of this route; the gateway never converts protocols.");
    typed_error(&msg, "invalid_request_error", Some("protocol_mismatch"))
}

fn requested_model(body: &Value) -> String {
    body.get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

/// Ordered target resolution shared by the Messages-family and chat routes.
#[allow(clippy::result_large_err)]
fn resolve(
    st: &AppState,
    guard: &mut Guard,
    ingress: Ingress,
    requested: &str,
    inbound: &HeaderMap,
) -> Result<Vec<Target>, Response> {
    match routing::resolve(&st.settings, ingress, requested, selectors(inbound)) {
        Resolved::Mismatch { model, provider } => Err(refuse(
            guard,
            400,
            "protocol",
            &mismatch_body(&model, &provider, ingress),
        )),
        Resolved::BadRequest(msg) => Err(refuse(guard, 400, "target_selection", &error_body(&msg))),
        Resolved::Targets(t) => Ok(t),
    }
}

pub async fn messages(State(st): St, headers: HeaderMap, raw: Bytes) -> Response {
    match preamble(&st, &headers, &raw, Route::Messages) {
        Ok(body) => fallthrough(&st, Route::Messages, Ingress::Messages, &headers, body).await,
        Err(r) => r,
    }
}

pub async fn chat_completions(State(st): St, headers: HeaderMap, raw: Bytes) -> Response {
    match preamble(&st, &headers, &raw, Route::ChatCompletions) {
        Ok(body) => fallthrough(&st, Route::ChatCompletions, Ingress::Chat, &headers, body).await,
        Err(r) => r,
    }
}

async fn fallthrough(
    st: &Arc<AppState>,
    route: Route,
    ingress: Ingress,
    inbound: &HeaderMap,
    body: Value,
) -> Response {
    let mut guard = start(st, route, &body);
    let targets = match resolve(st, &mut guard, ingress, &requested_model(&body), inbound) {
        Ok(t) => t,
        Err(r) => return r,
    };
    let mut attempts: Vec<Attempt> = Vec::new();
    let mut upstream_called = false;
    for (i, t) in targets.iter().enumerate() {
        let last = i + 1 == targets.len();
        let planned = match &t.plan {
            Ok(c) => prepare(st, c, route, inbound, &body).map(|p| (c, p)),
            Err(a) => Err(a.clone()),
        };
        let (c, prepared) = match planned {
            Ok(cp) => cp,
            Err(a) => {
                attempts.push(a);
                continue;
            }
        };
        upstream_called = true;
        let resp = match dispatch(st, &mut guard, c, i, &prepared).await {
            Ok(r) => r,
            Err((_, stage, message)) => {
                attempts.push(attempt(&c.provider, stage, 502, message));
                continue;
            }
        };
        if resp.status().is_success() {
            return relay_stream(into_upstream(resp), guard, prepared.local.as_ref(), Some(c));
        }
        let status = resp.status();
        let content_type = resp.headers().get("content-type").cloned();
        let raw = match resp.bytes().await {
            Ok(raw) => raw,
            Err(e) => {
                let message = format!("Upstream error body could not be read: {e}");
                attempts.push(attempt(
                    &c.provider,
                    "upstream_response",
                    status.as_u16(),
                    message,
                ));
                continue;
            }
        };
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

pub async fn count_tokens(State(st): St, headers: HeaderMap, raw: Bytes) -> Response {
    let body = match preamble(&st, &headers, &raw, Route::CountTokens) {
        Ok(b) => b,
        Err(r) => return r,
    };
    let mut guard = start(&st, Route::CountTokens, &body);
    let requested = requested_model(&body);
    let candidate = match resolve(&st, &mut guard, Ingress::CountTokens, &requested, &headers) {
        Ok(t) => t
            .into_iter()
            .find_map(|t| t.plan.ok())
            .filter(|c| !c.model.is_empty()),
        Err(r) => return r,
    };
    let Some(c) = candidate else {
        let shown = if requested.is_empty() {
            "(none)"
        } else {
            &requested
        };
        let msg = format!("Model not found for /v1/messages/count_tokens: {shown}");
        let b = typed_error(&msg, "invalid_request_error", Some("model_not_found"));
        return refuse(&mut guard, 404, "model_resolution", &b);
    };
    relay_one(&st, guard, &c, Route::CountTokens, &headers, &body).await
}

/// 0013: strict allowlist, provider-credential-only bearer, verbatim relay.
pub async fn systemone(State(st): St, headers: HeaderMap, raw: Bytes) -> Response {
    let body = match preamble(&st, &headers, &raw, Route::Systemone) {
        Ok(b) => b,
        Err(r) => return r,
    };
    let mut guard = start(&st, Route::Systemone, &body);
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
        let msg = format!("Model not found for /v1/systemone: {shown}");
        let b = typed_error(&msg, "invalid_request_error", Some("model_not_found"));
        return refuse(&mut guard, 404, "model_resolution", &b);
    };
    if p.baseurl.is_none() || p.apikey.is_none() {
        let msg = format!(
            "Provider {} is not configured with a baseurl and credential.",
            p.name
        );
        let b = typed_error(&msg, "api_error", Some("provider_not_configured"));
        return refuse(&mut guard, 503, "provider_config", &b);
    }
    // The leg is unused: `Route::Systemone` has its own URL and header case in `upstream`.
    let c = Candidate {
        provider: p,
        model,
        leg: Leg::OpenAiChat,
    };
    relay_one(&st, guard, &c, Route::Systemone, &headers, &body).await
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
    crate::telemetry::sink::iso8601(SystemTime::now())
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
        let (a, b) = (request_id(), request_id());
        assert_ne!(a, b);
        assert_eq!(a.len(), 33);
        assert_eq!(a[..17], b[..17]);
    }
}
