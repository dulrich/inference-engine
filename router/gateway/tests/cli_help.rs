//! A bare `--help` (alone or after a subcommand) prints usage and exits 0 before any config,
//! key or socket is touched.
use std::process::Command;

#[test]
fn help_prints_usage_and_touches_nothing() {
    let dir = std::env::temp_dir().join(format!("ir-cli-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    for args in [
        &["--help"][..],
        &["-h"][..],
        &["serve", "--help"][..],
        &["healthcheck", "--help"][..],
        &["check-catalog", "--help"][..],
    ] {
        let out = Command::new(env!("CARGO_BIN_EXE_inference-router"))
            .args(args)
            .current_dir(&dir)
            .env_clear()
            .output()
            .expect("spawn inference-router");
        assert_eq!(out.status.code(), Some(0), "{args:?}: {out:?}");
        let text = String::from_utf8_lossy(&out.stdout);
        assert!(text.contains("usage: inference-router"), "{args:?}: {text}");
    }
    assert_eq!(
        std::fs::read_dir(&dir).unwrap().count(),
        0,
        "help created files"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn unknown_subcommand_is_still_an_error() {
    let out = Command::new(env!("CARGO_BIN_EXE_inference-router"))
        .arg("bogus")
        .env_clear()
        .output()
        .expect("spawn inference-router");
    assert_eq!(out.status.code(), Some(1), "{out:?}");
}
