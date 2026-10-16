//! `bun run check:recipes` (plan bench-dashboard-rust-port WP-5): the served-recipe and
//! visibility/gateway conformance gate over `llama/config.yaml`. Flags: `--config <path>`
//! (default: the repo's own `llama/config.yaml`), `--visibility <path>`, `--gateway <path>`
//! (default: resolved relative to the config), each also as `--<name>=<path>`. Exit 0 when
//! conforming, 1 on any finding or an unreadable/unparseable config.
use std::io::Write;

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    match bd_server::check_recipes::run(&argv) {
        Ok((lines, code)) => {
            let mut out = std::io::stdout().lock();
            for line in lines {
                let _ = writeln!(out, "{line}");
            }
            let _ = out.flush();
            std::process::exit(code);
        }
        Err(message) => {
            eprintln!("error: {message}");
            std::process::exit(1);
        }
    }
}
