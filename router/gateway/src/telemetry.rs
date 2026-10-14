//! Telemetry delegate seam (WP-4 fills this file in with the 0006 sink; WP-2 ships the no-op).
//!
//! Lifecycle per proxied request (every route but `/health`/`/v1/models`):
//! 1. `Telemetry::request_start` at request start, before target resolution and before any
//!    upstream call — the returned handle may spawn its own ≤1/s progress ticker from here.
//! 2. `RequestTelemetry::resolved` once per upstream attempt (provider/model known).
//! 3. `RequestTelemetry::upstream_chunk` for every upstream response-body chunk, read-only, in
//!    order, before any local-leg byte injection (success bodies and relayed final error bodies).
//! 4. `RequestTelemetry::finalize` exactly once: success end-of-stream, upstream error,
//!    timeout, gateway refusal, or client disconnect (driven by `Drop` of the gateway's guard).

use crate::config::{Env, Settings};
use crate::relay::Route;
use axum::http::HeaderMap;
use serde_json::Value;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

pub struct RequestStart<'a> {
    pub request_id: &'a str,
    pub route: Route,
    /// Client-requested model string, verbatim (`None` when absent / not a string).
    pub requested_model: Option<&'a str>,
    /// Ingress headers (for `metadata.user_id`-adjacent derivation; contains credentials —
    /// the sink must allowlist, never dump).
    pub headers: &'a HeaderMap,
    /// The parsed client body (`metadata.user_id`, first `system` block for `cc_is_subagent`).
    /// `None` on `/v1/systemone`, whose `state`/`questions` must never reach the sink.
    pub body: Option<&'a Value>,
    pub streaming: bool,
    pub started_at: Instant,
    pub started_wall: SystemTime,
}

pub struct Resolution<'a> {
    /// Provider `name` (0006 `leg`).
    pub provider_name: &'a str,
    /// Node provider token: `anthropic` | `openai` | `systemone`.
    pub provider_kind: &'a str,
    /// Bare upstream model id.
    pub model: &'a str,
    pub upstream_url: &'a str,
    /// 0-based attempt index within the fallthrough list.
    pub attempt: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OutcomeKind {
    /// Upstream 2xx relayed to completion.
    Completed,
    /// Final upstream non-2xx (relayed verbatim or enveloped) or upstream connect/read failure.
    UpstreamError,
    /// `UPSTREAM_TIMEOUT_MS` elapsed waiting for headers or between body chunks.
    Timeout,
    /// The gateway answered without (successfully) calling upstream: auth is excluded,
    /// so this is model/protocol/credential refusal or a bad body.
    Refused,
    /// The client went away before the response finished.
    ClientDisconnect,
}

#[derive(Clone, Debug)]
pub struct Outcome {
    pub kind: OutcomeKind,
    /// Status sent (or about to be sent) to the client; `499` for a disconnect before headers.
    pub status: u16,
    /// 0006 `error.{stage,message}`.
    pub error: Option<(String, String)>,
    pub elapsed: Duration,
    /// Time from request start to upstream response headers, when they arrived.
    pub upstream_headers_after: Option<Duration>,
}

pub trait RequestTelemetry: Send + 'static {
    fn resolved(&mut self, resolution: &Resolution<'_>);
    fn upstream_chunk(&mut self, chunk: &[u8]);
    fn finalize(self: Box<Self>, outcome: &Outcome);
}

pub trait Telemetry: Send + Sync + 'static {
    fn request_start(&self, start: &RequestStart<'_>) -> Box<dyn RequestTelemetry>;
}

/// The delegate the composition point (`app.rs`) installs. WP-4 returns its sink here
/// (reading the `ROUTER_TELEMETRY_*` knobs through `env`).
pub fn delegate(_settings: &Settings, _env: Env) -> Arc<dyn Telemetry> {
    Arc::new(NoopTelemetry)
}

/// WP-2 no-op.
pub struct NoopTelemetry;

struct NoopRequest;

impl RequestTelemetry for NoopRequest {
    fn resolved(&mut self, _resolution: &Resolution<'_>) {}
    fn upstream_chunk(&mut self, _chunk: &[u8]) {}
    fn finalize(self: Box<Self>, _outcome: &Outcome) {}
}

impl Telemetry for NoopTelemetry {
    fn request_start(&self, _start: &RequestStart<'_>) -> Box<dyn RequestTelemetry> {
        Box::new(NoopRequest)
    }
}
