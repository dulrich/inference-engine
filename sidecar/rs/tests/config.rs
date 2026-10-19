//! `read_config`: every default from sidecar/README.md "Environment" / "Test seams", strict
//! integer knobs (an invalid value is an error naming the variable), and the unset-vs-empty
//! SIDECAR_BIND_EXTRA.

use codex_sidecar::config::{read_config, Pinned, INBAND_ITEM_TYPES};
use std::collections::HashMap;

fn env(pairs: &[(&str, &str)]) -> HashMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

#[test]
fn defaults_match_readme_environment() {
    let c = read_config(&env(&[("HOME", "/home/u")])).unwrap();
    assert_eq!(c.key, "");
    assert_eq!(c.port, 4877);
    assert_eq!(c.bind_extra, "172.17.0.1");
    assert_eq!(c.personas, ["codex-a", "codex-b"]);
    assert_eq!(c.state_dir, "/home/u/.local/state/codex-sidecar");
    assert_eq!(
        c.cwd_fallback,
        "/home/u/.local/state/codex-sidecar/empty-cwd"
    );
    assert_eq!(c.log_dir, "/home/u/.local/state/codex-sidecar/logs");
    assert_eq!(c.log_keep_days, 14);
    assert_eq!(c.outbox_path, "");
    assert_eq!(c.context_window_tokens, 258_400);
    assert_eq!(c.app_server_cmd, "");
    assert_eq!(
        [
            c.ping_ms,
            c.batch_ms,
            c.turn_timeout_ms,
            c.backoff_ms,
            c.bind_retry_ms,
            c.bind_retry_budget_ms
        ],
        [15_000, 200, 300_000, 1000, 500, 60_000]
    );
}

#[test]
fn integers_trim_and_empty_takes_the_default() {
    let c = read_config(&env(&[
        ("SIDECAR_PORT", "1000"),
        ("SIDECAR_PING_MS", " 42 "),
        ("SIDECAR_BATCH_MS", ""),
        ("SIDECAR_BACKOFF_MS", "  "),
    ]))
    .unwrap();
    assert_eq!(c.port, 1000);
    assert_eq!(c.ping_ms, 42);
    assert_eq!(c.batch_ms, 200, "empty takes the default");
    assert_eq!(c.backoff_ms, 1000, "whitespace-only takes the default");
}

#[test]
fn invalid_integers_are_errors_naming_the_variable() {
    for (k, v) in [
        ("SIDECAR_PORT", "0x10"),
        ("SIDECAR_PORT", "65536"),
        ("SIDECAR_PORT", "-1"),
        ("SIDECAR_PING_MS", "1.5"),
        ("SIDECAR_TURN_TIMEOUT_MS", "1e3"),
        ("SIDECAR_BACKOFF_MS", "+5"),
        ("SIDECAR_LOG_KEEP_DAYS", "abc"),
        ("SIDECAR_LOG_KEEP_DAYS", "4294967296"),
        ("SIDECAR_CONTEXT_WINDOW_TOKENS", "Infinity"),
        ("SIDECAR_CONTEXT_WINDOW_TOKENS", "18446744073709551616"),
    ] {
        let e = read_config(&env(&[(k, v)])).expect_err(v);
        assert!(e.starts_with(&format!("{k}: ")), "{e}");
        assert!(e.contains(v), "{e}");
    }
}

#[test]
fn bind_extra_unset_vs_empty_and_string_fallbacks() {
    let c = read_config(&env(&[
        ("SIDECAR_BIND_EXTRA", ""),
        ("SIDECAR_PERSONAS", " a , ,b,"),
        ("SIDECAR_STATE_DIR", "/s"),
        ("SIDECAR_LOG_DIR", ""),
        ("SIDECAR_KEY", "k"),
    ]))
    .unwrap();
    assert_eq!(c.bind_extra, "", "empty means skip the extra bind");
    assert_eq!(c.personas, ["a", "b"]);
    assert_eq!(c.log_dir, "/s/logs", "an empty string takes the || default");
    assert_eq!(c.key, "k");
    assert!(
        !format!("{c:?}").contains("\"k\""),
        "the key is never in Debug output"
    );
    let none = read_config(&env(&[("SIDECAR_PERSONAS", "")])).unwrap();
    assert!(
        none.personas.is_empty(),
        "?? keeps an empty SIDECAR_PERSONAS"
    );
}

#[test]
fn pinned_files_load_from_the_sidecar_dir() {
    let p = Pinned::load().expect("pinned files");
    assert!(p.dir.join("preamble.txt").is_file());
    assert!(!p.preamble.ends_with('\n'));
    assert!(p.thread_config.is_object());
    assert_eq!(INBAND_ITEM_TYPES.len(), 5);
    assert!(INBAND_ITEM_TYPES.contains(&"contextCompaction"));
}
