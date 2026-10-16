//! `inference-dashboard`: the API + SPA on one port (`apps/api/src/index.ts`, the default).
//! `inference-dashboard --healthcheck` probes `/health` by body on the configured port.
//! `inference-dashboard ingest [--dir <path>|--dir=<path>] [--rebuild]` is
//! `apps/api/src/ingest/cli.ts` (plan Decision 6).
use bd_server::app::router;
use bd_server::config::{listen_port, Config};
use bd_server::db::Db;
use bd_server::health;
use bd_server::http::{AppState, Shared};
use std::process::ExitCode;
use std::sync::Arc;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let cfg = Config::from_env();
    if args.iter().skip(1).any(|a| a == "--healthcheck") {
        let healthy = listen_port(cfg.port).is_ok_and(|p| health::check(p, health::BUDGET));
        return if healthy {
            ExitCode::SUCCESS
        } else {
            ExitCode::FAILURE
        };
    }
    // `connection.ts` opens the DB at import time, before the CLI parses its arguments.
    let db = match Db::open(&cfg.db_path) {
        Ok(db) => db,
        Err(e) => {
            eprintln!("inference-dashboard: db {e}");
            return ExitCode::FAILURE;
        }
    };
    if args.get(1).map(String::as_str) == Some("ingest") {
        let code = bd_server::ingest::cli(&db, &args[2..], &cfg.records_dir);
        return ExitCode::from(code);
    }
    let port = match listen_port(cfg.port) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("inference-dashboard: {e}");
            return ExitCode::FAILURE;
        }
    };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(r) => r,
        Err(e) => {
            eprintln!("inference-dashboard: runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    let state = Arc::new(AppState { cfg, db });
    match runtime.block_on(serve(state, port)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("inference-dashboard: {e}");
            ExitCode::FAILURE
        }
    }
}

async fn shutdown() {
    let term = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut s) => {
                s.recv().await;
            }
            Err(_) => std::future::pending::<()>().await,
        }
    };
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        () = term => {}
    }
}

async fn serve(state: Shared, port: u16) -> std::io::Result<()> {
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port)).await?;
    let bound = listener.local_addr()?.port();
    // Bind scope is decided by the compose publish; the process listens on all interfaces.
    println!("Bench Dashboard API listening on port {bound}");
    axum::serve(listener, router(state))
        .with_graceful_shutdown(shutdown())
        .await
}
