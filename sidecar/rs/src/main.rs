//! codex-sidecar entry point. WP-2 placeholder: it reads the config and the pinned files the
//! way the server will, then exits non-zero — the server main loop lands in WP-3.

use codex_sidecar::config::{self, Pinned};
use std::process::ExitCode;

fn main() -> ExitCode {
    let cfg = config::read_config(&config::process_env());
    if cfg.key.is_empty() {
        eprintln!("codex-sidecar: SIDECAR_KEY is required");
        return ExitCode::from(2);
    }
    if let Err(e) = Pinned::load() {
        eprintln!("codex-sidecar: cannot read pinned files: {e}");
        return ExitCode::from(2);
    }
    eprintln!(
        "codex-sidecar {}: not yet serving (Rust port WP-2 placeholder; the server lands in WP-3)",
        config::VERSION
    );
    ExitCode::from(1)
}
