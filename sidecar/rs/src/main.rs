//! codex-sidecar entry point: boot, print the
//! listening line, and shut down gracefully on SIGTERM/SIGINT (a second signal, or a
//! shutdown hung past 5 s, exits at once). Any boot failure exits 1.

use codex_sidecar::config::{self, Pinned};
use codex_sidecar::sidecar;
use std::io::Write;
use std::process::ExitCode;
use tokio::signal::unix::{signal, SignalKind};

fn main() -> ExitCode {
    let rt = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("codex-sidecar: {e}");
            return ExitCode::from(1);
        }
    };
    rt.block_on(run())
}

async fn run() -> ExitCode {
    let cfg = match config::read_config(&config::process_env()) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("codex-sidecar: {e}");
            return ExitCode::from(1);
        }
    };
    let pinned = match Pinned::load() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("codex-sidecar: {e}");
            return ExitCode::from(1);
        }
    };
    let h = match sidecar::start(cfg, pinned).await {
        Ok(h) => h,
        Err(e) => {
            eprintln!("codex-sidecar: {e}");
            return ExitCode::from(1);
        }
    };
    let mut out = std::io::stdout();
    let _ = out.write_all(h.banner().as_bytes());
    let _ = out.flush();
    let (Ok(mut term), Ok(mut int)) = (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
    ) else {
        return ExitCode::from(1);
    };
    tokio::select! {
        _ = term.recv() => {}
        _ = int.recv() => {}
    }
    tokio::spawn(async move {
        tokio::select! {
            _ = term.recv() => {}
            _ = int.recv() => {}
            _ = tokio::time::sleep(std::time::Duration::from_millis(5000)) => {}
        }
        std::process::exit(0);
    });
    h.close().await;
    ExitCode::from(0)
}
