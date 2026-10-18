//! The upstream half of the relay: URL rule, header build, body preparation, send + retry,
//! and the one failure → telemetry-kind mapping every handler shares.
//!
//! Inbound header policy (B4): a header the gateway *forwards* is copied as its raw
//! `HeaderValue`, so forwarding cannot fail and nothing is dropped. A header the gateway must
//! *read as text* (the OAuth bearer, `anthropic-version`/`anthropic-beta`) is read through
//! [`hdr`], where a value that is not visible ASCII counts as absent — that is the one explicit
//! skip, and it only ever turns a request into the absent-header case (the OpenAI model list,
//! or the documented 401 when no caller credential exists).

use super::Route;
use crate::app::AppState;
use crate::auth;
use crate::config::{Credential, Provider};
use crate::local_leg::{self, LocalLegContext};
use crate::routing::{Attempt, Candidate, Leg};
use crate::telemetry::OutcomeKind;
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use bytes::Bytes;
use futures_util::StreamExt;
use serde_json::Value;
use std::io;
use std::sync::Arc;
use std::time::Duration;

use super::ByteStream;

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

impl UpstreamFailure {
    /// The single failure → (telemetry kind, stage) mapping (P1).
    pub(crate) fn classify(&self) -> (OutcomeKind, &'static str) {
        if self.timeout {
            (OutcomeKind::Timeout, "upstream_timeout")
        } else {
            (OutcomeKind::UpstreamError, "upstream_connect")
        }
    }
}

/// Text read of an inbound header; empty or non-visible-ASCII counts as absent (module doc).
pub(crate) fn hdr<'a>(h: &'a HeaderMap, name: &str) -> Option<&'a str> {
    h.get(name)
        .and_then(|v| v.to_str().ok())
        .filter(|v| !v.is_empty())
}

/// Raw inbound header for forwarding; empty counts as absent, any other value is forwarded.
fn fwd<'a>(h: &'a HeaderMap, name: &str) -> Option<&'a HeaderValue> {
    h.get(name).filter(|v| !v.is_empty())
}

fn bearer_of(raw: &[u8]) -> HeaderValue {
    HeaderValue::from_bytes(&[b"Bearer ", raw].concat())
        .expect("`Bearer ` + bytes of an existing HeaderValue is a valid HeaderValue")
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

pub(crate) fn upstream_url(c: &Candidate, route: Route) -> Option<String> {
    let base = c.provider.baseurl.as_deref()?.trim_end_matches('/');
    let v1 = if base.ends_with("/v1") {
        base.to_string()
    } else {
        format!("{base}/v1")
    };
    Some(match (c.leg, route) {
        (_, Route::Systemone) => format!("{base}/systemone"),
        (Leg::OpenAiChat, _) => format!("{base}/chat/completions"),
        (_, Route::CountTokens) => format!("{v1}/messages/count_tokens"),
        (Leg::LocalMessages, _) => format!("{base}/messages"),
        (Leg::Anthropic, _) => format!("{base}/v1/messages"),
    })
}

pub(crate) fn attempt(
    p: &Arc<Provider>,
    stage: &'static str,
    status: u16,
    message: String,
) -> Attempt {
    Attempt {
        provider: p.clone(),
        stage,
        message,
        status: Some(status),
        details: None,
    }
}

fn no_credential(p: &Arc<Provider>) -> Attempt {
    let msg = "has no configured credential; the caller's credential is never forwarded.";
    config_error(p, format!("Provider {} {msg}", p.name))
}

/// A `provider_config` 503: the gateway's own configuration cannot serve this target.
fn config_error(p: &Arc<Provider>, message: String) -> Attempt {
    attempt(p, "provider_config", 503, message)
}

/// A gateway-constructed header value from config (a key, the default version). A value that
/// is not header-safe (e.g. a stray newline in an env var) is a recorded refusal, never a panic.
#[allow(clippy::result_large_err)]
fn config_value(p: &Arc<Provider>, what: &str, s: &str) -> Result<HeaderValue, Attempt> {
    HeaderValue::from_str(s).map_err(|_| {
        config_error(
            p,
            format!("Provider {} {what} is not a valid header value.", p.name),
        )
    })
}

/// Upstream header set, built from scratch per route and leg (0001, 0013).
#[allow(clippy::result_large_err)] // internal, cold path
pub(crate) fn upstream_headers(
    st: &AppState,
    c: &Candidate,
    route: Route,
    inbound: &HeaderMap,
) -> Result<HeaderMap, Attempt> {
    let p = &c.provider;
    let mut h = HeaderMap::new();
    h.insert("content-type", HeaderValue::from_static("application/json"));
    let copy = |h: &mut HeaderMap, name: &'static str| {
        if let Some(v) = fwd(inbound, name) {
            h.insert(HeaderName::from_static(name), v.clone());
        }
    };
    if route == Route::Systemone {
        // 0013: provider bearer only; no caller header is ever forwarded.
        let key = p.apikey.as_deref().ok_or_else(|| no_credential(p))?;
        h.insert(
            "authorization",
            config_value(p, "credential", &format!("Bearer {key}"))?,
        );
        return Ok(h);
    }
    if c.leg == Leg::OpenAiChat {
        copy(&mut h, "openai-organization");
        copy(&mut h, "openai-project");
    } else {
        let version = match fwd(inbound, "anthropic-version") {
            Some(v) => v.clone(),
            None => config_value(p, "anthropic version", &st.settings.anthropic_version)?,
        };
        h.insert("anthropic-version", version);
        copy(&mut h, "anthropic-beta");
        if route == Route::Messages || p.credential == Credential::CallerOauthPassthrough {
            copy(&mut h, "user-agent");
        }
    }
    match (&p.apikey, p.credential) {
        (None, Credential::CallerOauthPassthrough) if c.leg == Leg::Anthropic => {
            if let Some(key) = fwd(inbound, "x-api-key") {
                h.insert("x-api-key", key.clone());
                h.insert("authorization", bearer_of(key.as_bytes()));
            } else if let Some(token) = hdr(inbound, "authorization").and_then(auth::bearer) {
                // 0001: an OAuth passthrough bearer is sent as `authorization` alone.
                h.insert("authorization", bearer_of(token.as_bytes()));
            } else {
                // Pin 9: refuse clearly with 401, no upstream call.
                return Err(attempt(p, NO_CALLER_CRED_STAGE, 401, NO_CALLER_CRED.into()));
            }
        }
        (Some(key), Credential::ProviderKey) => {
            let leg = c.leg;
            h.insert(
                "authorization",
                config_value(p, "credential", &format!("Bearer {key}"))?,
            );
            if leg == Leg::Anthropic {
                h.insert("x-api-key", config_value(p, "credential", key)?);
            }
        }
        // Unkeyed provider-key legs (and, unreachably, a keyed passthrough: refused at load).
        _ => return Err(no_credential(p)),
    }
    Ok(h)
}

pub(crate) const NO_CALLER_CRED_STAGE: &str = "caller_credential";
const NO_CALLER_CRED: &str = "No caller credential for the anthropic provider.";

pub(crate) fn is_streaming(body: &Value) -> bool {
    body.get("stream") == Some(&Value::Bool(true))
}

pub(crate) struct Prepared {
    pub url: String,
    headers: HeaderMap,
    body: Bytes,
    pub local: Option<LocalLegContext>,
}

#[allow(clippy::result_large_err)]
pub(crate) fn prepare(
    st: &AppState,
    c: &Candidate,
    route: Route,
    inbound: &HeaderMap,
    body: &Value,
) -> Result<Prepared, Attempt> {
    let url = upstream_url(c, route).ok_or_else(|| {
        config_error(
            &c.provider,
            format!("Provider {} has no baseurl.", c.provider.name),
        )
    })?;
    let headers = upstream_headers(st, c, route, inbound)?;
    let mut out = body.clone();
    out["model"] = Value::String(c.model.clone());
    let local = (c.leg == Leg::LocalMessages).then(|| LocalLegContext {
        provider: c.provider.clone(),
        model: c.model.clone(),
        route,
        streaming: is_streaming(body),
    });
    if let Some(ctx) = &local {
        out = local_leg::transform_body(out, ctx);
    }
    Ok(Prepared {
        url,
        headers,
        // serde_json `arbitrary_precision` keeps number text (B2): only rewritten fields change.
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

/// One retry on the same target after 150 ms only when the connection was never established
/// (`is_connect`): the request cannot have reached upstream, so a metered POST never runs
/// twice. A reset after the request was written, a timeout, or any status is not retried (S2).
pub(crate) async fn send(
    st: &AppState,
    p: &Prepared,
) -> Result<reqwest::Response, UpstreamFailure> {
    match send_once(st, p).await {
        Err(e) if e.is_connect() => {
            tokio::time::sleep(Duration::from_millis(150)).await;
            send_once(st, p).await.map_err(failure)
        }
        r => r.map_err(failure),
    }
}

pub(crate) fn into_upstream(resp: reqwest::Response) -> UpstreamResponse {
    let status = resp.status();
    let headers = relay_headers(resp.headers());
    UpstreamResponse {
        status,
        headers,
        body: Box::pin(resp.bytes_stream().map(|r| r.map_err(io_err))),
    }
}
