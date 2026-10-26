//! codex-sidecar entry point: boot, print the
//! listening line, and shut down gracefully on SIGTERM/SIGINT (fleetkit `shutdown`: a
//! watchdog thread exits 0 once the 5 s drain deadline elapses). Any boot failure exits 1.

use codex_sidecar::config::{self, Pinned};
use codex_sidecar::sidecar;
use std::io::Write;
use std::process::ExitCode;

const USAGE: &str = "usage: codex-sidecar [--help]\n\nAnthropic-Messages front end over `codex app-server`. Takes no arguments; configured entirely\nby environment (SIDECAR_KEY, ...). Runs until SIGTERM/SIGINT.\n  -h, --help  print this usage and exit 0\n";

fn main() -> ExitCode {
    // Help is answered before the runtime, the environment config or any child process exists.
    if std::env::args().skip(1).any(|a| a == "--help" || a == "-h") {
        print!("{USAGE}");
        return ExitCode::SUCCESS;
    }
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
    // Installed first: nothing below may block a SIGTERM past the drain deadline.
    let sd = fleetkit::shutdown::install(fleetkit::shutdown::DEFAULT_DRAIN);
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
    sd.triggered().await;
    // `close()` is the graceful step (stop personas, land in-flight requests, cut the rest);
    // the watchdog exits 0 at the deadline if it hangs.
    sd.drain(h.close()).await;
    ExitCode::from(0)
}
