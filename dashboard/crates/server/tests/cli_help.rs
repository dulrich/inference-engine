//! A bare `--help` (anywhere in the argument list) prints usage and exits 0 without opening the
//! database or binding a port, for the server and for `check-recipes`.
use std::path::PathBuf;
use std::process::{Command, Output};

fn run(exe: &str, tag: &str, args: &[&str]) -> (Output, PathBuf) {
    let dir = std::env::temp_dir().join(format!("id-cli-{}-{tag}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let out = Command::new(exe)
        .args(args)
        .current_dir(std::env::temp_dir())
        .env("APP_DB_PATH", dir.join("data/app.db"))
        // A port nothing listens on: a regression to `serve` would bind it rather than collide.
        .env("PORT", "1")
        .output()
        .expect("spawn");
    (out, dir)
}

#[test]
fn server_help_prints_usage_and_touches_nothing() {
    for (tag, args) in [
        ("long", &["--help"][..]),
        ("short", &["-h"][..]),
        ("ingest", &["ingest", "--help"][..]),
        ("ingest-dir", &["ingest", "--dir", "x", "-h"][..]),
    ] {
        let (out, dir) = run(env!("CARGO_BIN_EXE_inference-dashboard"), tag, args);
        assert_eq!(out.status.code(), Some(0), "{args:?}: {out:?}");
        let text = String::from_utf8_lossy(&out.stdout);
        assert!(
            text.contains("usage: inference-dashboard"),
            "{args:?}: {text}"
        );
        assert!(!dir.exists(), "{args:?} created {}", dir.display());
    }
}

#[test]
fn check_recipes_help_prints_usage() {
    let (out, _dir) = run(env!("CARGO_BIN_EXE_check-recipes"), "cr", &["--help"]);
    assert_eq!(out.status.code(), Some(0), "{out:?}");
    assert!(String::from_utf8_lossy(&out.stdout).contains("usage: check-recipes"));
}
