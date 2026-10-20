//! Live serving residency (`GET /api/serving/live`, plans/serving-control-redesign.md WP-6).
//! Read from the host status collector's `serving-feed-YYYY-MM-DD.jsonl` (`serving-feed-v1`,
//! `control/status_collector.py`); the collector reads only llama-swap `/running` and ONNX
//! `/health`, and this API makes no outbound HTTP at all.
use super::*;

pub const SERVING_FEED_SCHEMA: &str = "serving-feed-v1";

pub const SERVING_LIVE_TIER: Export = Export::out("ServingLiveTier", Node::Enum(&["gpu", "onnx"]));

pub const SERVING_LIVE_EVENT_KIND: Export =
    Export::out("ServingLiveEventKind", Node::Enum(&["load", "unload"]));

/// One feed line. Snapshot lines carry `available` + `models` | `reason`; transition lines
/// carry `model`. Unknown keys pass through (the collector may add fields).
pub const SERVING_FEED_LINE: Export = Export::out(
    "ServingFeedLine",
    passthrough(&[
        f("schema", &Node::Lit(Lit::Str(SERVING_FEED_SCHEMA))),
        f("kind", &Node::Enum(&["snapshot", "load", "unload"])),
        f("ts", &STR),
        f("tier", &Node::Ref(&SERVING_LIVE_TIER)),
        f("available", &Node::Optional(&BOOL)),
        f("reason", &NULLISH_STR),
        f("models", &Node::Optional(&Node::Array(&UNKNOWN_RECORD))),
        f("model", &NULLISH_STR),
    ]),
);

pub const SERVING_LIVE_MODEL: Export = Export::out(
    "ServingLiveModel",
    obj(&[
        f("model", &STR),
        f("state", &NULL_STR),
        f("ttl", &NULL_NUM),
        f("preload", &NULL_BOOL),
        f("idle_unload_seconds", &NULL_NUM),
    ]),
);

pub const SERVING_LIVE_TIER_STATUS: Export = Export::out(
    "ServingLiveTierStatus",
    Node::DiscUnion {
        key: "available",
        options: &[
            &obj(&[
                f("available", &Node::Lit(Lit::Bool(true))),
                f("sampled_at", &STR),
                f("models", &Node::Array(&Node::Ref(&SERVING_LIVE_MODEL))),
            ]),
            &obj(&[
                f("available", &Node::Lit(Lit::Bool(false))),
                f("sampled_at", &NULL_STR),
                f("reason", &STR),
            ]),
        ],
    },
);

pub const SERVING_LIVE_EVENT: Export = Export::out(
    "ServingLiveEvent",
    obj(&[
        f("ts", &STR),
        f("tier", &Node::Ref(&SERVING_LIVE_TIER)),
        f("kind", &Node::Ref(&SERVING_LIVE_EVENT_KIND)),
        f("model", &STR),
    ]),
);

pub const SERVING_LIVE_UNAVAILABLE_REASON: Export = Export::out(
    "ServingLiveUnavailableReason",
    Node::Enum(&["disabled", "unreadable", "empty", "stale"]),
);

/// 60 s: four 15 s collector polls. Three missed polls is the floor the plan asks for; one more
/// absorbs a slow `/health` (5 s timeout per tier) without flapping.
pub const SERVING_LIVE_STALE_MS: i64 = 60_000;

pub const SERVING_LIVE_RESPONSE: Export = Export::out(
    "ServingLiveResponse",
    Node::DiscUnion {
        key: "available",
        options: &[
            &obj(&[
                f("available", &Node::Lit(Lit::Bool(true))),
                f("generated_at", &STR),
                f("file", &STR),
                f("gpu", &Node::Ref(&SERVING_LIVE_TIER_STATUS)),
                f("onnx", &Node::Ref(&SERVING_LIVE_TIER_STATUS)),
                f("recent", &Node::Array(&Node::Ref(&SERVING_LIVE_EVENT))),
            ]),
            &obj(&[
                f("available", &Node::Lit(Lit::Bool(false))),
                f("reason", &Node::Ref(&SERVING_LIVE_UNAVAILABLE_REASON)),
            ]),
        ],
    },
);
