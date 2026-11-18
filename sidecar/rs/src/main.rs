//! codex-sidecar entry point: boot, print the
//! listening line, and shut down gracefully on SIGTERM/SIGINT (fleetkit `shutdown`: a
//! watchdog thread exits 0 once the 5 s drain deadline elapses). Any boot failure exits 1.
//!
//! Generations (README "Generations"): sockets inherited from systemd are served instead of
//! binding, readiness is `READY=1` to `NOTIFY_SOCKET` after boot, and SIGUSR1 retires the
//! process: stop admitting, let in-flight requests end, exit 0 (`RETIRED`). At the retire
//! deadline every open stream is cut and the process exits `DEADLINE_EXIT` within the 5 s
//! drain bound. SIGTERM/SIGINT keep their meaning at any point, a retire included.

use codex_sidecar::config::{self, Pinned};
use codex_sidecar::sidecar;
use std::io::Write;
use std::process::ExitCode;

const USAGE: &str = "usage: codex-sidecar [--help]\n\nAnthropic-Messages front end over `codex app-server`. Takes no arguments; configured entirely\nby environment (SIDECAR_KEY, ...). Runs until SIGTERM/SIGINT (exit 0 within 5 s);\nSIGUSR1 retires it (stop admitting, finish in-flight requests, exit 0; exit 3 when\nSIDECAR_RETIRE_DEADLINE_MS cut them).\n  -h, --help  print this usage and exit 0\n";

/// The exit status of a retire that hit its deadline (a clean retire exits 0).
const DEADLINE_EXIT: u8 = 3;

fn main() -> ExitCode {
    // Help is answered before the runtime, the environment config or any child process exists.
    if std::env::args().skip(1).any(|a| a == "--help" || a == "-h") {
        print!("{USAGE}");
        return ExitCode::SUCCESS;
    }
    // `--build`/`--version` print the build-time id (build.rs); answered before any I/O.
    if std::env::args()
        .skip(1)
        .any(|a| a == "--build" || a == "--version")
    {
        println!("{}", env!("FLEET_BUILD_ID"));
        return ExitCode::SUCCESS;
    }
    // Before any thread exists: these edit the environment so no codex child inherits them.
    let inherited = sidecar::take_inherited_listeners();
    sidecar::capture_notify_socket();
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
    rt.block_on(run(inherited))
}

enum Outcome {
    Term,
    Retired,
    Deadline,
}

async fn run(inherited: Vec<std::net::TcpListener>) -> ExitCode {
    // Installed first: nothing below may block a SIGTERM past the drain deadline.
    let sd = fleetkit::shutdown::install(fleetkit::shutdown::DEFAULT_DRAIN);
    // Installed before readiness: SIGUSR1's default action would kill the process.
    let mut usr1 =
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::user_defined1()) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("codex-sidecar: SIGUSR1 handler: {e}");
                return ExitCode::from(1);
            }
        };
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
    let deadline = std::time::Duration::from_millis(cfg.retire_deadline_ms);
    let mut h = match sidecar::start_with(cfg, pinned, inherited).await {
        Ok(h) => h,
        Err(e) => {
            eprintln!("codex-sidecar: {e}");
            return ExitCode::from(1);
        }
    };
    let mut out = std::io::stdout();
    let _ = out.write_all(h.banner().as_bytes());
    let _ = out.flush();
    // Startup checks passed and every listener is serving: only now is the generation ready.
    sidecar::sd_notify("READY=1\nSTATUS=accepting");
    let outcome = tokio::select! {
        () = sd.triggered() => Outcome::Term,
        _ = usr1.recv() => retire(&mut h, &sd, deadline).await,
    };
    match outcome {
        Outcome::Term => {
            // `close()` is the graceful step (stop personas, land in-flight requests, cut the
            // rest); the watchdog exits 0 at the deadline if it hangs.
            sd.drain(h.close()).await;
            ExitCode::from(0)
        }
        Outcome::Retired => {
            h.log("retired", serde_json::json!({}));
            eprintln!("codex-sidecar: retired");
            // Nothing is in flight: stopping the personas cuts nothing.
            sd.drain(h.close()).await;
            ExitCode::from(0)
        }
        Outcome::Deadline => {
            h.log(
                "retire-deadline",
                serde_json::json!({ "deadlineMs": deadline.as_millis() as u64 }),
            );
            eprintln!("codex-sidecar: retire deadline reached; cutting open streams");
            // The same 5 s bound SIGTERM has, with the deadline's own exit status.
            std::thread::spawn(|| {
                std::thread::sleep(fleetkit::shutdown::DEFAULT_DRAIN);
                std::process::exit(i32::from(DEADLINE_EXIT));
            });
            let _ = tokio::time::timeout(fleetkit::shutdown::DEFAULT_DRAIN, h.close()).await;
            ExitCode::from(DEADLINE_EXIT)
        }
    }
}

/// SIGUSR1: stop admitting, acknowledge, wait for in-flight requests (bounded by the retire
/// deadline). A SIGTERM/SIGINT at any point wins.
async fn retire(
    h: &mut sidecar::Handle,
    sd: &fleetkit::shutdown::Shutdown,
    deadline: std::time::Duration,
) -> Outcome {
    let started = std::time::Instant::now();
    h.begin_retire();
    tokio::select! {
        () = sd.triggered() => return Outcome::Term,
        () = h.stopped_accepting() => {}
    }
    // The acknowledgment the installer waits for: no listener is held any more.
    h.log(
        "retiring",
        serde_json::json!({ "deadlineMs": deadline.as_millis() as u64 }),
    );
    sidecar::sd_notify("STATUS=retiring");
    eprintln!("codex-sidecar: retiring (stopped accepting)");
    let left = deadline.saturating_sub(started.elapsed());
    tokio::select! {
        () = sd.triggered() => Outcome::Term,
        () = h.settled() => Outcome::Retired,
        () = tokio::time::sleep(left) => Outcome::Deadline,
    }
}
