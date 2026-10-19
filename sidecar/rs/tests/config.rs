//! `readConfig` parity (B1): every default from sidecar/README.md "Environment" / "Test seams",
//! JS `Number()` leniency with non-finite fallback, and the unset-vs-empty SIDECAR_BIND_EXTRA.

use codex_sidecar::config::{read_config, Pinned, INBAND_ITEM_TYPES};
use std::collections::HashMap;

fn env(pairs: &[(&str, &str)]) -> HashMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

#[test]
fn defaults_match_config_mjs() {
    let c = read_config(&env(&[("HOME", "/home/u")]));
    assert_eq!(c.key, "");
    assert_eq!(c.port, 4877.0);
    assert_eq!(c.bind_extra, "172.17.0.1");
    assert_eq!(c.personas, ["codex-a", "codex-b"]);
    assert_eq!(c.state_dir, "/home/u/.local/state/codex-sidecar");
    assert_eq!(
        c.cwd_fallback,
        "/home/u/.local/state/codex-sidecar/empty-cwd"
    );
    assert_eq!(c.log_dir, "/home/u/.local/state/codex-sidecar/logs");
    assert_eq!(c.log_keep_days, 14.0);
    assert_eq!(c.outbox_path, "");
    assert_eq!(c.context_window_tokens, 258_400.0);
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
        [15_000.0, 200.0, 300_000.0, 1000.0, 500.0, 60_000.0]
    );
}

#[test]
fn numbers_follow_js_number_and_fall_back_only_when_non_finite() {
    let c = read_config(&env(&[
        ("SIDECAR_PORT", "1e3"),
        ("SIDECAR_PING_MS", " 42 "),
        ("SIDECAR_BATCH_MS", ""),
        ("SIDECAR_BACKOFF_MS", "0x10"),
        ("SIDECAR_LOG_KEEP_DAYS", "abc"),
        ("SIDECAR_TURN_TIMEOUT_MS", "Infinity"),
        ("SIDECAR_BIND_RETRY_MS", "1.5"),
    ]));
    assert_eq!(c.port, 1000.0);
    assert_eq!(c.ping_ms, 42.0);
    assert_eq!(c.batch_ms, 0.0, "Number(\"\") is 0, which is finite");
    assert_eq!(c.backoff_ms, 16.0);
    assert_eq!(c.log_keep_days, 14.0, "NaN falls back");
    assert_eq!(c.turn_timeout_ms, 300_000.0, "Infinity falls back");
    assert_eq!(c.bind_retry_ms, 1.5);
}

#[test]
fn bind_extra_unset_vs_empty_and_string_fallbacks() {
    let c = read_config(&env(&[
        ("SIDECAR_BIND_EXTRA", ""),
        ("SIDECAR_PERSONAS", " a , ,b,"),
        ("SIDECAR_STATE_DIR", "/s"),
        ("SIDECAR_LOG_DIR", ""),
        ("SIDECAR_KEY", "k"),
    ]));
    assert_eq!(c.bind_extra, "", "empty means skip the extra bind");
    assert_eq!(c.personas, ["a", "b"]);
    assert_eq!(c.log_dir, "/s/logs", "an empty string takes the || default");
    assert_eq!(c.key, "k");
    assert!(
        !format!("{c:?}").contains("\"k\""),
        "the key is never in Debug output"
    );
    let none = read_config(&env(&[("SIDECAR_PERSONAS", "")]));
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
