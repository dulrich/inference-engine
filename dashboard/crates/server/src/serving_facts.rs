//! The `serving-facts-v1` reader: the research dashboard (eval-harness) writes the file, this
//! side mounts it `:ro` at `SERVING_FACTS_PATH` and serves it as one `GET /api/serving/facts`
//! body (`ServingFactsResponse`). One read of the file and one hash of the llama config per
//! response, so timing, measured and availability are always one generation. Every state is a
//! 200; read-only, never writes.
use crate::jsrt;
pub use bd_domain::contract::ServingFactsUnavailable as Unavailable;
use bd_domain::contract::SERVING_FACTS_FILE;
use bd_domain::validate;
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// A validated, revision-current facts file (unknown fields already stripped).
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase")]
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
    let facts: Facts = serde_json::from_value(data).map_err(|_| Unavailable::Invalid)?;
    if jsrt::date_parse(&facts.generated_at).is_none() {
        return Err(Unavailable::Invalid);
    }
    let config = std::fs::read(llama_config_path).map_err(|_| Unavailable::StaleConfig)?;
    if sha256_hex(&config) != facts.serving_config_revision {
        return Err(Unavailable::StaleConfig);
    }
    Ok(facts)
}

/// The `ServingFactsResponse` body for one read.
pub fn response(facts: &Result<Facts, Unavailable>, research_port: u16) -> Value {
    match facts {
        Ok(f) => json!({
            "available": true,
            "generatedAt": f.generated_at,
            "servingConfigRevision": f.serving_config_revision,
            "timing": f.timing,
            "measured": f.measured,
            "researchPort": research_port,
        }),
        Err(r) => json!({ "available": false, "reason": r, "researchPort": research_port }),
    }
}

/// `GET /api/serving/facts`: exactly one read, then the one body built from it.
pub fn serve(facts_path: Option<&str>, llama_config_path: &str, research_port: u16) -> Value {
    response(
        &read_serving_facts(facts_path, llama_config_path),
        research_port,
    )
}

#[cfg(test)]
#[path = "serving_facts_tests.rs"]
mod tests;
