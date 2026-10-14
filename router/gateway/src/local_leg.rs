//! Local-leg delegate seam (WP-3 fills this file in; WP-2 ships the no-op).
//!
//! Invoked only for `claude-local`-style providers (`localLeg: "native-messages"`) on
//! Anthropic-Messages ingress: `/v1/messages` and `/v1/messages/count_tokens`
//! (`ctx.route` tells them apart). Chat-completions ingress to the same provider never calls it.

use crate::config::{Env, Provider, Settings};
use crate::relay::{ByteStream, PendingUpstream, Route};
use futures_util::StreamExt;
use serde_json::Value;
use std::sync::Arc;

/// Everything the delegate may read about the request. Owned, so stream layers can keep it.
#[derive(Clone, Debug)]
pub struct LocalLegContext {
    /// The resolved provider; `effort_kwargs` / `max_output_tokens` are its raw config values.
    pub provider: Arc<Provider>,
    /// The resolved bare upstream model id (already written into the body's `model`).
    pub model: String,
    pub route: Route,
    /// The client body's `stream == true`.
    pub streaming: bool,
}

/// Whether the gateway waits for the upstream response head before answering the client.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HeaderMode {
    /// Node behaviour: relay the upstream status/headers once they arrive, then
    /// `wrap_stream` the body. Upstream errors keep the normal 0003 relay.
    Hold,
    /// Answer `200` + SSE headers immediately and let `early_stream` produce the whole body
    /// (it owns surfacing a late upstream error in-stream). Only honoured for streaming
    /// `/v1/messages`; ignored elsewhere.
    Early,
}

pub trait LocalLeg: Send + Sync + 'static {
    /// The upstream body to send, given the client body with `model` already rewritten.
    fn transform_body(&self, body: Value, ctx: &LocalLegContext) -> Value;

    fn header_mode(&self, ctx: &LocalLegContext) -> HeaderMode;

    /// `Hold` mode, successful upstream: wraps the (telemetry-tapped) upstream body. May inject
    /// bytes (e.g. `event: ping`); must otherwise pass bytes through unchanged.
    fn wrap_stream(&self, upstream: ByteStream, ctx: &LocalLegContext) -> ByteStream;

    /// `Early` mode: the client body, given the in-flight upstream call. The upstream body
    /// inside `pending` is already telemetry-tapped.
    fn early_stream(&self, pending: PendingUpstream, ctx: &LocalLegContext) -> ByteStream;
}

/// The delegate the composition point (`app.rs`) installs. WP-3 returns its impl here.
pub fn delegate(_settings: &Settings, _env: Env) -> Arc<dyn LocalLeg> {
    Arc::new(NoopLocalLeg)
}

/// WP-2 no-op: plain relay.
pub struct NoopLocalLeg;

impl LocalLeg for NoopLocalLeg {
    fn transform_body(&self, body: Value, _ctx: &LocalLegContext) -> Value {
        body
    }

    fn header_mode(&self, _ctx: &LocalLegContext) -> HeaderMode {
        HeaderMode::Hold
    }

    fn wrap_stream(&self, upstream: ByteStream, _ctx: &LocalLegContext) -> ByteStream {
        upstream
    }

    fn early_stream(&self, pending: PendingUpstream, _ctx: &LocalLegContext) -> ByteStream {
        Box::pin(
            futures_util::stream::once(pending).flat_map(|r| -> ByteStream {
                match r {
                    Ok(resp) => resp.body,
                    Err(_) => Box::pin(futures_util::stream::empty()),
                }
            }),
        )
    }
}
