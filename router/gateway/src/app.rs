//! The single composition point: state assembly + route table. WP-3/WP-4 change only their
//! own module's `delegate()` factory, never this file.

use crate::config::{Env, Settings};
use crate::local_leg::{self, LocalLeg};
use crate::relay;
use crate::telemetry::{self, Telemetry};
use axum::extract::DefaultBodyLimit;
use axum::routing::{get, post};
use axum::Router;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

pub struct AppState {
    pub settings: Settings,
    pub client: reqwest::Client,
    pub local_leg: Arc<dyn LocalLeg>,
    pub telemetry: Arc<dyn Telemetry>,
}

fn client(settings: &Settings) -> Result<reqwest::Client, String> {
    // No auto-decompression features are compiled in, so bytes relay verbatim; no proxy env.
    let mut b = reqwest::Client::builder().no_proxy();
    if settings.upstream_timeout_ms > 0 {
        // Node's undici dispatcher applies the one timeout to headers and to body idle gaps.
        b = b.read_timeout(Duration::from_millis(settings.upstream_timeout_ms));
    }
    b.build().map_err(|e| format!("http client: {e}"))
}

pub fn state_with(
    settings: Settings,
    local_leg: Arc<dyn LocalLeg>,
    telemetry: Arc<dyn Telemetry>,
) -> Result<Arc<AppState>, String> {
    Ok(Arc::new(AppState {
        client: client(&settings)?,
        settings,
        local_leg,
        telemetry,
    }))
}

pub fn state(settings: Settings, env: Env) -> Result<Arc<AppState>, String> {
    let local = local_leg::delegate(&settings, env);
    let tel = telemetry::delegate(&settings, env);
    state_with(settings, local, tel)
}

pub fn router(state: Arc<AppState>) -> Router {
    let limit = state.settings.body_limit;
    Router::new()
        .route("/v1/messages", post(relay::messages))
        .route("/v1/messages/count_tokens", post(relay::count_tokens))
        .route("/v1/chat/completions", post(relay::chat_completions))
        .route("/v1/systemone", post(relay::systemone))
        .route("/v1/models", get(relay::models))
        .route("/health", get(relay::health))
        .layer(DefaultBodyLimit::max(limit))
        .with_state(state)
}

/// Bind and serve until the process ends. `on_bound` sees the real address (tests use port 0).
pub async fn serve(state: Arc<AppState>, on_bound: impl FnOnce(SocketAddr)) -> Result<(), String> {
    let addr = format!("{}:{}", state.settings.host, state.settings.port);
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| format!("bind {addr}: {e}"))?;
    on_bound(listener.local_addr().map_err(|e| e.to_string())?);
    axum::serve(listener, router(state))
        .await
        .map_err(|e| e.to_string())
}
