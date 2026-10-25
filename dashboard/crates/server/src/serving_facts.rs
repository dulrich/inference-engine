//! The `serving-facts-v1` reader (plan `research-to-eval-harness` WP-4): the research dashboard
//! writes the file, this side mounts it `:ro` at `SERVING_FACTS_PATH` and serves its `timing`
//! (`GET /api/serving/timing`) and `measured` (`GET /api/serving/measured`) lists, plus the typed
//! availability (`GET /api/serving/facts-status`). Every state is a 200: an unavailable file
//! makes both lists `[]`, and the status route names the reason. Read-only; never writes.
use crate::jsrt;
use bd_domain::contract::SERVING_FACTS_FILE;
use bd_domain::validate;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// Why the facts are unavailable (`ServingFactsUnavailableReason`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Unavailable {
    /// `SERVING_FACTS_PATH` unset or empty.
    Disabled,
    /// The file does not exist (the research side has not exported yet).
    Absent,
    /// Any other read failure.
    Unreadable,
    /// Not JSON, or not a `serving-facts-v1` envelope.
    Invalid,
    /// `servingConfigRevision` is not the SHA-256 of this side's `LLAMA_CONFIG_PATH` bytes (or
    /// those bytes cannot be read, so the revision cannot be confirmed).
    StaleConfig,
}

impl Unavailable {
    pub fn as_str(self) -> &'static str {
        match self {
            Unavailable::Disabled => "disabled",
            Unavailable::Absent => "absent",
            Unavailable::Unreadable => "unreadable",
            Unavailable::Invalid => "invalid",
            Unavailable::StaleConfig => "stale-config",
        }
    }
}

/// A validated, revision-current facts file (unknown fields already stripped).
#[derive(Debug, Clone, PartialEq)]
pub struct Facts {
    pub generated_at: String,
    pub serving_config_revision: String,
    pub timing: Value,
    pub measured: Value,
}

/// Lowercase hex SHA-256 of `bytes`.
pub fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Read, validate and revision-check the facts file. Read per request (the file and the
/// config are small; no cache to go stale).
pub fn read_serving_facts(
    facts_path: Option<&str>,
    llama_config_path: &str,
) -> Result<Facts, Unavailable> {
    let Some(path) = facts_path.filter(|p| !p.is_empty()) else {
        return Err(Unavailable::Disabled);
    };
    let bytes = match std::fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err(Unavailable::Absent),
        Err(_) => return Err(Unavailable::Unreadable),
    };
    let text = std::str::from_utf8(&bytes).map_err(|_| Unavailable::Invalid)?;
    let parsed = jsrt::json_parse(text).map_err(|_| Unavailable::Invalid)?;
    let data =
        validate::parse(&SERVING_FACTS_FILE.node, &parsed).map_err(|_| Unavailable::Invalid)?;
    let field = |k: &str| data.get(k).cloned().unwrap_or(Value::Null);
    let generated_at = field("generatedAt")
        .as_str()
        .unwrap_or_default()
        .to_string();
    if jsrt::date_parse(&generated_at).is_none() {
        return Err(Unavailable::Invalid);
    }
    let revision = field("servingConfigRevision")
        .as_str()
        .unwrap_or_default()
        .to_string();
    let config = std::fs::read(llama_config_path).map_err(|_| Unavailable::StaleConfig)?;
    if sha256_hex(&config) != revision {
        return Err(Unavailable::StaleConfig);
    }
    Ok(Facts {
        generated_at,
        serving_config_revision: revision,
        timing: field("timing"),
        measured: field("measured"),
    })
}

/// `ServingTimingRow[]`, `[]` when unavailable.
pub fn timing(facts: &Result<Facts, Unavailable>) -> Value {
    facts
        .as_ref()
        .map_or_else(|_| json!([]), |f| f.timing.clone())
}

/// `ServingMeasuredEntry[]`, `[]` when unavailable.
pub fn measured(facts: &Result<Facts, Unavailable>) -> Value {
    facts
        .as_ref()
        .map_or_else(|_| json!([]), |f| f.measured.clone())
}

/// `ServingFactsStatusResponse`.
pub fn status(facts: &Result<Facts, Unavailable>) -> Value {
    match facts {
        Ok(f) => json!({
            "available": true,
            "generatedAt": f.generated_at,
            "servingConfigRevision": f.serving_config_revision,
        }),
        Err(r) => json!({ "available": false, "reason": r.as_str() }),
    }
}

#[cfg(test)]
#[path = "serving_facts_tests.rs"]
mod tests;
