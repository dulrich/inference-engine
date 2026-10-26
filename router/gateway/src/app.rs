//! The single composition point: state assembly + route table.

use crate::catalog::{self, Holder};
use crate::config::{Env, Settings};
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
    /// The live model catalog; handlers take one `snapshot()` per request.
    pub catalog: Arc<Holder>,
    pub client: reqwest::Client,
    pub telemetry: Arc<dyn Telemetry>,
}

fn client(settings: &Settings) -> Result<reqwest::Client, String> {
    // No auto-decompression features are compiled in, so bytes relay verbatim; no proxy env.
    // Dead-peer detection that never bounds legitimate silence: a slow upstream (model load,
    // prefill) still ACKs, so neither keepalive (idle connection: 15 s + 3 × 5 s) nor
    // TCP_USER_TIMEOUT (unacknowledged written data, 30 s) fires; a vanished WG peer does.
    let mut b = reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_millis(settings.upstream_connect_timeout_ms))
        .tcp_keepalive(Duration::from_secs(15))
        .tcp_keepalive_interval(Duration::from_secs(5))
        .tcp_keepalive_retries(3)
        .tcp_user_timeout(Duration::from_secs(30))
        .pool_idle_timeout(Duration::from_secs(60));
    if settings.upstream_timeout_ms > 0 {
        // The one timeout bounds the wait for headers and every idle gap in the body.
        b = b.read_timeout(Duration::from_millis(settings.upstream_timeout_ms));
    }
    b.build().map_err(|e| format!("http client: {e}"))
}

pub fn state_with(
    settings: Settings,
    catalog: Arc<Holder>,
    telemetry: Arc<dyn Telemetry>,
) -> Result<Arc<AppState>, String> {
    Ok(Arc::new(AppState {
        client: client(&settings)?,
        settings,
        catalog,
        telemetry,
    }))
}

/// Production state: the catalog from `GATEWAY_CATALOG_PATH` (missing/invalid = fatal).
pub fn state(settings: Settings, env: Env) -> Result<Arc<AppState>, String> {
    let path = catalog::path_from_env(env);
    let holder = Holder::load(settings.providers.clone(), path)?;
    let tel = telemetry::from_env(env);
    state_with(settings, Arc::new(holder), tel)
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
    serve_until(
        state,
        fleetkit::shutdown::Shutdown::without_watchdog(),
        on_bound,
    )
    .await
}

/// `serve` that drains on `shutdown` (production passes the signal-installed handle).
pub async fn serve_until(
    state: Arc<AppState>,
    shutdown: fleetkit::shutdown::Shutdown,
    on_bound: impl FnOnce(SocketAddr),
) -> Result<(), String> {
    let addr = format!("{}:{}", state.settings.host, state.settings.port);
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| format!("bind {addr}: {e}"))?;
    on_bound(listener.local_addr().map_err(|e| e.to_string())?);
    state.catalog.spawn_reloader(catalog::POLL_INTERVAL);
    let server = axum::serve(listener, router(state)).with_graceful_shutdown(shutdown.triggered());
    // In-flight proxied streams are bounded by the drain deadline; the watchdog exits at 5 s regardless.
    match shutdown
        .drain(std::future::IntoFuture::into_future(server))
        .await
    {
        Some(r) => r.map_err(|e| e.to_string()),
        None => Ok(()),
    }
}
