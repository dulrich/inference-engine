//! `servingFacts.ts`: the `serving-facts-v1` file the research dashboard (eval-harness
//! `inference_research`) writes and this serving dashboard reads `:ro` (plan
//! `research-to-eval-harness` WP-3/WP-4), plus the typed availability of that file.
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

/// `GET /api/serving/facts-status`: always 200.
pub const SERVING_FACTS_STATUS_RESPONSE: Export = Export::out(
    "ServingFactsStatusResponse",
    Node::DiscUnion {
        key: "available",
        options: &[
            &obj(&[
                f("available", &Node::Lit(Lit::Bool(true))),
                f("generatedAt", &STR),
                f("servingConfigRevision", &STR),
            ]),
            &obj(&[
                f("available", &Node::Lit(Lit::Bool(false))),
                f("reason", &Node::Ref(&SERVING_FACTS_UNAVAILABLE_REASON)),
            ]),
        ],
    },
);
