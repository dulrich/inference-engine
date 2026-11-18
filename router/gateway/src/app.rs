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

/// After a retire stops accepting, how long a just-accepted connection has to send its first
/// request before axum's graceful shutdown closes idle connections.
const ACCEPT_GRACE: Duration = Duration::from_millis(500);

/// How a generation's serve ended (`main` maps it to the exit status).
#[derive(Debug, PartialEq, Eq)]
pub enum Outcome {
    /// SIGTERM/SIGINT: the fleet drain contract (5 s), exit 0.
    Term,
    /// SIGUSR1 retire completed: nothing was in flight any more, exit 0.
    Retired,
    /// The retire deadline cut the open streams, exit `generation::DEADLINE_EXIT`.
    Deadline,
}

/// The generation serve (README/AGENTS "Generations"): serve on every inherited socket (or
/// bind `host:port` as `serve_until` does when there are none), declare readiness, and run
/// until SIGTERM/SIGINT (`shutdown`, unchanged drain) or a SIGUSR1 retire: every accept loop
/// ends (idle keep-alive connections close, a busy one after its in-flight response), the
/// acknowledgment fires once no listener is held, then the retire waits for every connection
/// to end, bounded by `retire_deadline_ms`.
pub async fn serve_generation(
    state: Arc<AppState>,
    shutdown: fleetkit::shutdown::Shutdown,
    inherited: Vec<std::net::TcpListener>,
    mut on_bound: impl FnMut(SocketAddr),
) -> Result<Outcome, String> {
    use crate::generation::{sd_notify, Counted, DEADLINE_EXIT};
    use std::sync::atomic::{AtomicUsize, Ordering};
    // Installed before readiness: SIGUSR1's default action would kill the process.
    let mut usr1 = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::user_defined1())
        .map_err(|e| format!("SIGUSR1 handler: {e}"))?;
    let mut listeners = Vec::new();
    if inherited.is_empty() {
        let addr = format!("{}:{}", state.settings.host, state.settings.port);
        let l = tokio::net::TcpListener::bind(&addr)
            .await
            .map_err(|e| format!("bind {addr}: {e}"))?;
        listeners.push(l);
    } else {
        for l in inherited {
            l.set_nonblocking(true)
                .map_err(|e| format!("inherited socket: {e}"))?;
            listeners.push(
                tokio::net::TcpListener::from_std(l)
                    .map_err(|e| format!("inherited socket: {e}"))?,
            );
        }
    }
    let deadline = Duration::from_millis(state.settings.retire_deadline_ms);
    let activation = state.settings.activation.clone();
    state.catalog.spawn_reloader(catalog::POLL_INTERVAL);
    let open = Arc::new(AtomicUsize::new(0));
    // `accept_tx` ends the accept loops (each listener closes its socket); `stop_tx` then
    // starts axum's graceful shutdown (idle connections close, busy ones after their response).
    let (accept_tx, accept_rx) = tokio::sync::watch::channel(false);
    let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
    let mut tasks = Vec::new();
    for l in listeners {
        on_bound(l.local_addr().map_err(|e| e.to_string())?);
        let counted = Counted::new(l, &open, accept_rx.clone());
        let (mut rx, term) = (stop_rx.clone(), shutdown.triggered());
        let server =
            axum::serve(counted, router(state.clone())).with_graceful_shutdown(async move {
                tokio::select! {
                    _ = rx.wait_for(|stop| *stop) => {}
                    () = term => {}
                }
            });
        tasks.push(tokio::spawn(std::future::IntoFuture::into_future(server)));
    }
    // Startup checks passed and every listener serves: only now is the generation ready.
    sd_notify("READY=1\nSTATUS=accepting");
    let all_done = |tasks: Vec<tokio::task::JoinHandle<std::io::Result<()>>>| async move {
        for t in tasks {
            let _ = t.await;
        }
    };
    let aborts: Vec<_> = tasks.iter().map(|t| t.abort_handle()).collect();
    tokio::select! {
        () = shutdown.triggered() => {
            // In-flight proxied streams are bounded by the drain deadline; the watchdog exits at 5 s regardless.
            shutdown.drain(all_done(tasks)).await;
            return Ok(Outcome::Term);
        }
        _ = usr1.recv() => {}
    }
    let started = std::time::Instant::now();
    let _ = accept_tx.send(true);
    while open.load(Ordering::SeqCst) > 0 {
        tokio::select! {
            () = shutdown.triggered() => {
                shutdown.drain(all_done(tasks)).await;
                return Ok(Outcome::Term);
            }
            () = tokio::time::sleep(Duration::from_millis(10)) => {}
        }
    }
    // The acknowledgment the installer waits for: no listener is held any more. Never
    // STOPPING=1 (it would arm TimeoutStopSec against the retire deadline).
    eprintln!(
        "{}",
        json_line("retiring", &activation, deadline.as_millis() as u64)
    );
    sd_notify("STATUS=retiring");
    eprintln!("inference-router: retiring (stopped accepting)");
    // A connection accepted just before the stop gets this long to send its first request
    // before graceful shutdown treats it as idle.
    tokio::select! {
        () = shutdown.triggered() => {
            shutdown.drain(all_done(tasks)).await;
            return Ok(Outcome::Term);
        }
        () = tokio::time::sleep(ACCEPT_GRACE) => {}
    }
    let _ = stop_tx.send(true);
    let left = deadline.saturating_sub(started.elapsed());
    let mut settled = Box::pin(all_done(tasks));
    tokio::select! {
        () = shutdown.triggered() => {
            shutdown.drain(settled).await;
            Ok(Outcome::Term)
        }
        () = &mut settled => {
            eprintln!("{}", json_line("retired", &activation, deadline.as_millis() as u64));
            eprintln!("inference-router: retired");
            Ok(Outcome::Retired)
        }
        () = tokio::time::sleep(left) => {
            eprintln!("{}", json_line("retire-deadline", &activation, deadline.as_millis() as u64));
            eprintln!("inference-router: retire deadline reached; cutting open streams");
            // The same 5 s bound SIGTERM has, with the deadline's own exit status.
            std::thread::spawn(|| {
                std::thread::sleep(fleetkit::shutdown::DEFAULT_DRAIN);
                std::process::exit(i32::from(DEADLINE_EXIT));
            });
            for a in aborts {
                a.abort();
            }
            Ok(Outcome::Deadline)
        }
    }
}

fn json_line(event: &str, activation: &str, deadline_ms: u64) -> String {
    serde_json::json!({"event": event, "activation": activation, "deadlineMs": deadline_ms})
        .to_string()
}
