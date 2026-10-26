//! `servingFacts.ts`: the `serving-facts-v1` file the research dashboard (eval-harness
//! `inference_research`) writes and this serving dashboard reads `:ro` (plan
//! `research-to-eval-harness` WP-3/WP-4), plus the `GET /api/serving/facts` response over it.
use super::*;

pub const SERVING_FACTS_SCHEMA_V1: &str = "serving-facts-v1";

/// One served alias that has a measured experiment (`sweepCell.alias` + experiment `id`).
pub const SERVING_MEASURED_ENTRY: Export = Export::out(
    "ServingMeasuredEntry",
    obj(&[f("alias", &STR), f("experimentId", &STR)]),
);

/// The file envelope. Unknown extra fields are stripped (additive evolution).
pub const SERVING_FACTS_FILE: Export = Export::out(
    "ServingFactsFile",
    obj(&[
        f("schema", &Node::Lit(Lit::Str(SERVING_FACTS_SCHEMA_V1))),
        f("generatedAt", &STR),
        f("servingConfigRevision", &STR),
        f("timing", &Node::Array(&Node::Ref(&SERVING_TIMING_ROW))),
        f(
            "measured",
            &Node::Array(&Node::Ref(&SERVING_MEASURED_ENTRY)),
        ),
    ]),
);

/// Why the facts are unavailable — the one Rust source of the wire spelling (serde kebab-case).
/// The contract enum below lists the same names for `gen-ts`; a test pins the two together.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ServingFactsUnavailable {
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

impl ServingFactsUnavailable {
    pub const ALL: [ServingFactsUnavailable; 5] = [
        Self::Disabled,
        Self::Absent,
        Self::Unreadable,
        Self::Invalid,
        Self::StaleConfig,
    ];
}

pub const SERVING_FACTS_UNAVAILABLE_REASON: Export = Export::out(
    "ServingFactsUnavailableReason",
    Node::Enum(&[
        "disabled",
        "absent",
        "unreadable",
        "invalid",
        "stale-config",
    ]),
);

/// `GET /api/serving/facts`: always 200, one read of the file per response (timing, measured
/// and availability are one generation). `researchPort` is the research dashboard's port from
/// server config; the SPA joins it to its own `location.hostname`.
pub const SERVING_FACTS_RESPONSE: Export = Export::out(
    "ServingFactsResponse",
    Node::DiscUnion {
        key: "available",
        options: &[
            &obj(&[
                f("available", &Node::Lit(Lit::Bool(true))),
                f("generatedAt", &STR),
                f("servingConfigRevision", &STR),
                f("timing", &Node::Array(&Node::Ref(&SERVING_TIMING_ROW))),
                f(
                    "measured",
                    &Node::Array(&Node::Ref(&SERVING_MEASURED_ENTRY)),
                ),
                f("researchPort", &NONNEG_INT),
            ]),
            &obj(&[
                f("available", &Node::Lit(Lit::Bool(false))),
                f("reason", &Node::Ref(&SERVING_FACTS_UNAVAILABLE_REASON)),
                f("researchPort", &NONNEG_INT),
            ]),
        ],
    },
);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reason_enum_matches_serde_spelling() {
        let serde_names: Vec<String> = ServingFactsUnavailable::ALL
            .iter()
            .map(|r| {
                serde_json::to_value(r)
                    .unwrap()
                    .as_str()
                    .unwrap()
                    .to_string()
            })
            .collect();
        let Node::Enum(names) = SERVING_FACTS_UNAVAILABLE_REASON.node else {
            panic!("reason is an enum");
        };
        assert_eq!(serde_names, names.to_vec());
    }
}
