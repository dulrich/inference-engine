//! Port of sidecar/test/effort.test.mjs, case for case: suffix -> output_config.effort ->
//! thinking disabled -> family default, and never an error.

use codex_sidecar::protocol::{parse_model_id, resolve_effort, Effort};
use serde_json::{json, Value};

fn resolve(id: &str, body: Value) -> Effort {
    resolve_effort(parse_model_id(Some(&json!(id))).as_ref(), &body)
}

fn eff(e: &str, src: &'static str) -> Effort {
    Effort {
        effort: Some(e.into()),
        effort_source: src,
    }
}

#[test]
fn explicit_suffix_beats_conflicting_output_config_effort() {
    assert_eq!(
        resolve(
            "gpt-5.6-sol-low",
            json!({"output_config": {"effort": "high"}})
        ),
        eff("low", "suffix")
    );
    assert_eq!(
        resolve(
            "gpt-5.6-luna-high",
            json!({"output_config": {"effort": "low"}, "thinking": {"type": "disabled"}})
        ),
        eff("high", "suffix")
    );
    assert_eq!(
        resolve(
            "codex-b/gpt-5.6-sol-low",
            json!({"output_config": {"effort": "max"}})
        ),
        eff("low", "suffix")
    );
}

#[test]
fn bare_id_takes_request_effort_with_max_xhigh_folded() {
    for (want, effort) in [
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("max", "high"),
        ("xhigh", "high"),
    ] {
        assert_eq!(
            resolve("gpt-5.6-sol", json!({"output_config": {"effort": want}})),
            eff(effort, "request"),
            "{want}"
        );
    }
}

#[test]
fn thinking_disabled_floors_bare_id_at_low() {
    assert_eq!(
        resolve("gpt-5.6-sol", json!({"thinking": {"type": "disabled"}})),
        eff("low", "request")
    );
    assert_eq!(
        resolve(
            "gpt-5.6-sol",
            json!({"output_config": {"effort": "high"}, "thinking": {"type": "disabled"}})
        ),
        eff("high", "request")
    );
}

#[test]
fn bare_id_with_no_effort_field_keeps_family_default() {
    assert_eq!(resolve("gpt-5.6-luna", json!({})), eff("low", "default"));
    assert_eq!(resolve("gpt-5.6-sol", json!({})), eff("medium", "default"));
    assert_eq!(resolve("gpt-6-astra", json!({})), eff("medium", "default"));
    assert_eq!(resolve("gpt-6-sol", json!({})), eff("medium", "default"));
    assert_eq!(resolve("gpt-6.1-sol", json!({})), eff("medium", "default"));
    assert_eq!(resolve("gpt-6-luna", json!({})), eff("low", "default"));
    assert_eq!(
        resolve(
            "gpt-5.6-sol",
            json!({"thinking": {"type": "adaptive", "display": "omitted"}})
        ),
        eff("medium", "default")
    );
}

#[test]
fn unrecognised_effort_value_falls_to_default() {
    for bad in [
        json!("ultra"),
        json!("none"),
        json!(""),
        json!("HIGH"),
        json!(3),
        Value::Null,
        json!({}),
    ] {
        assert_eq!(
            resolve("gpt-5.6-sol", json!({"output_config": {"effort": bad}})),
            eff("medium", "default"),
            "{bad}"
        );
    }
    assert_eq!(
        resolve("gpt-5.6-sol", json!({"output_config": null})),
        eff("medium", "default")
    );
}

#[test]
fn thinking_enabled_budget_falls_to_default() {
    assert_eq!(
        resolve(
            "gpt-5.6-sol",
            json!({"thinking": {"type": "enabled", "budget_tokens": 512}})
        ),
        eff("medium", "default")
    );
    assert_eq!(
        resolve(
            "gpt-5.6-luna",
            json!({"thinking": {"type": "enabled", "budget_tokens": 24000}})
        ),
        eff("low", "default")
    );
    assert_eq!(
        resolve(
            "gpt-5.6-luna",
            json!({"thinking": {"type": "enabled", "budget_tokens": 512}, "output_config": {"effort": "high"}})
        ),
        eff("high", "request")
    );
}
