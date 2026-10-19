//! thread-config.mjs — is the thread the app-server actually started the one we declared?
//!
//! The instruction-source trim, and the declared-vs-reported feature comparison against the
//! app-server's own flag oracle. A declared key the oracle does not report is *unknown*, not a
//! mismatch. CONFIG drift detection only; `Persona.tripBoundary` is the runtime line.

use crate::js;
use serde_json::{Map, Value};

/// `verifyInstructionSources(sources)`: `None` when the trim held, else the problem string.
pub fn verify_instruction_sources(sources: Option<&Value>) -> Option<String> {
    let list: &[Value] = sources.and_then(Value::as_array).map_or(&[], Vec::as_slice);
    if list.len() > 1 {
        return Some(format!(
            "expected at most the persona AGENTS.md, got {} instruction sources",
            list.len()
        ));
    }
    let bad = list.iter().find(|s| {
        let t = match s {
            Value::String(x) => x.clone(),
            other => js::stringify(other),
        };
        !t.to_ascii_lowercase().ends_with("agents.md")
    })?;
    Some(format!(
        "unexpected instruction source: {}",
        js::utf16_prefix(&js::js_string(bad), 200)
    ))
}

/// `unified_exec` is pinned true by the model catalog's `shell_type` (WP-1 measurement,
/// 2026-09-14) — exempt.
pub const FEATURE_VERIFY_EXEMPT: &[&str] = &["unified_exec"];

#[derive(Debug, Clone, PartialEq)]
pub struct Mismatch {
    pub name: String,
    pub want: bool,
    pub got: bool,
    pub entry: Value,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct FeatureReadings {
    /// name -> effective flag, in declaration order.
    pub features: Map<String, Value>,
    pub unknown_features: Vec<String>,
    pub mismatches: Vec<Mismatch>,
}

/// `featureReadings(declared, reported)`.
pub fn feature_readings(declared: Option<&Value>, reported: Option<&Value>) -> FeatureReadings {
    let mut by_name: Vec<(&str, &Value)> = Vec::new();
    for e in reported.and_then(Value::as_array).into_iter().flatten() {
        if let Some(n) = js::prop(Some(e), "name").and_then(Value::as_str) {
            if !by_name.iter().any(|(k, _)| *k == n) {
                by_name.push((n, e));
            }
        }
    }
    let mut out = FeatureReadings::default();
    let decl = declared
        .filter(|d| d.is_object() || d.is_array())
        .map(js::entries)
        .unwrap_or_default();
    for (name, want_raw) in decl {
        if FEATURE_VERIFY_EXEMPT.contains(&name.as_str()) {
            continue;
        }
        let Some((_, entry)) = by_name.iter().find(|(k, _)| *k == name) else {
            out.unknown_features.push(name);
            continue;
        };
        let got = js::prop(Some(entry), "enabled") == Some(&Value::Bool(true));
        let want = *want_raw == Value::Bool(true);
        out.features.insert(name.clone(), Value::Bool(got));
        if got != want {
            out.mismatches.push(Mismatch {
                name,
                want,
                got,
                entry: (*entry).clone(),
            });
        }
    }
    out
}

/// `verifyFeatures(declared, reported)`: `None` when the effective flags match.
pub fn verify_features(declared: Option<&Value>, reported: Option<&Value>) -> Option<String> {
    let r = feature_readings(declared, reported);
    if r.mismatches.is_empty() {
        return None;
    }
    let parts: Vec<String> = r
        .mismatches
        .iter()
        .map(|m| format!("{}={} (declared {})", m.name, m.got, m.want))
        .collect();
    Some(format!("features not honoured: {}", parts.join(", ")))
}
