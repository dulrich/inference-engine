//! `routerStatus.ts`. The three responses are discriminated unions on `available`; the four
//! `.passthrough()` objects keep unknown sink keys.
use super::*;

pub const ROUTER_UNAVAILABLE_REASON_VALUES: &[&str] = &["disabled", "unreadable", "empty"];
pub const ROUTER_UNAVAILABLE_REASON: Export = Export::out(
    "RouterUnavailableReason",
    Node::Enum(ROUTER_UNAVAILABLE_REASON_VALUES),
);

/// Module-private in `routerStatus.ts` (only its type is exported).
pub const ROUTER_UNAVAILABLE: Export = Export::private(
    "RouterUnavailable",
    obj(&[
        f("available", &Node::Lit(Lit::Bool(false))),
        f("reason", &Node::Ref(&ROUTER_UNAVAILABLE_REASON)),
    ]),
);

pub const ROUTER_UPSTREAM_TIMINGS: Export = Export::out(
    "RouterUpstreamTimings",
    Node::Record {
        key: &STR,
        value: &NUM,
    },
);

const PHASE: Node = Node::Enum(&["prefill", "decode"]);

pub const ROUTER_REQUEST_RECORD: Export = Export::out(
    "RouterRequestRecord",
    passthrough(&[
        f("kind", &Node::Lit(Lit::Str("request"))),
        f("ts", &STR),
        f("reqId", &STR),
        f("route", &STR),
        f("method", &STR),
        f("leg", &NULL_STR),
        f("modelResolved", &NULL_STR),
        f("sessionId", &NULL_STR),
        f("isSubagent", &BOOL),
        f("stream", &BOOL),
        f("status", &NULL_NUM),
        f("responseTokens", &NULL_NUM),
        f("reasoningTokens", &NULL_NUM),
        f("ttftMs", &NULL_NUM),
        f("wallMs", &NULL_NUM),
        f("error", &Node::Nullable(&UNKNOWN)),
        f("modelRequested", &NULLISH_STR),
        f("providerType", &NULLISH_STR),
        f("sourceAdapterKey", &NULLISH_STR),
        f("upstreamUrl", &NULLISH_STR),
        f("upstreamFirstByteMs", &NULLISH_NUM),
        f("attemptSequence", &NULLISH_NUM),
        f("stopReason", &NULLISH_STR),
        f(
            "upstreamTimings",
            &Node::Optional(&Node::Nullable(&Node::Ref(&ROUTER_UPSTREAM_TIMINGS))),
        ),
        f(
            "usage",
            &Node::Optional(&Node::Nullable(&passthrough(&[
                f("input", &NULL_NUM),
                f("output", &NULL_NUM),
                f("cacheRead", &NULL_NUM),
                f("cacheWrite", &NULL_NUM),
            ]))),
        ),
    ]),
);

pub const ROUTER_PROGRESS_RECORD: Export = Export::out(
    "RouterProgressRecord",
    passthrough(&[
        f("kind", &Node::Lit(Lit::Str("progress"))),
        f("ts", &STR),
        f("reqId", &STR),
        f("sessionId", &NULL_STR),
        f("leg", &NULL_STR),
        f("modelResolved", &NULL_STR),
        f("phase", &PHASE),
        f("responseTokens", &NUM),
        f("reasoningTokens", &NUM),
        f("elapsedMs", &NUM),
    ]),
);

pub const ROUTER_IN_FLIGHT: Export = Export::out(
    "RouterInFlight",
    obj(&[
        f("reqId", &STR),
        f("leg", &NULL_STR),
        f("modelResolved", &NULL_STR),
        f("sessionId", &NULL_STR),
        f("phase", &PHASE),
        f("responseTokens", &NUM),
        f("reasoningTokens", &NUM),
        f("elapsedMs", &NUM),
        f("lastSeenTs", &STR),
        f("tokPerSec", &NULL_NUM),
    ]),
);

pub const ROUTER_LEG_SUMMARY: Export = Export::out(
    "RouterLegSummary",
    obj(&[
        f("leg", &NULL_STR),
        f("requests", &NONNEG_INT),
        f("errors", &NONNEG_INT),
        f("p50TtftMs", &NULL_NUM),
    ]),
);

const AVAILABLE: Node = Node::Lit(Lit::Bool(true));

pub const ROUTER_TELEMETRY_RESPONSE: Export = Export::out(
    "RouterTelemetryResponse",
    Node::DiscUnion {
        key: "available",
        options: &[
            &obj(&[
                f("available", &AVAILABLE),
                f("generatedAt", &STR),
                f("windowBytes", &POS_INT),
                f("file", &STR),
                f("recent", &Node::Array(&Node::Ref(&ROUTER_REQUEST_RECORD))),
                f("inFlight", &Node::Array(&Node::Ref(&ROUTER_IN_FLIGHT))),
                f("legs", &Node::Array(&Node::Ref(&ROUTER_LEG_SUMMARY))),
            ]),
            &Node::Ref(&ROUTER_UNAVAILABLE),
        ],
    },
);

pub const ROUTER_SLOT: Export = Export::out(
    "RouterSlot",
    obj(&[
        f("id", &NULL_NUM),
        f("nCtx", &NULL_NUM),
        f("isProcessing", &NULL_BOOL),
        f("promptTokens", &NULL_NUM),
        f("promptTokensCache", &NULL_NUM),
        f("cacheHitRatio", &NULL_NUM),
    ]),
);

pub const ROUTER_SLOT_RECIPE: Export = Export::out(
    "RouterSlotRecipe",
    obj(&[
        f("ctk", &NULL_STR),
        f("ctv", &NULL_STR),
        f("fa", &NULL_STR),
        f("reasoningBudget", &NULL_NUM),
        f("mtp", &NULL_BOOL),
        f("moeCpuLayers", &NULL_NUM),
    ]),
);

pub const ROUTER_SLOT_MODEL: Export = Export::out(
    "RouterSlotModel",
    obj(&[
        f("model", &STR),
        f("state", &NULL_STR),
        f("ttl", &NULL_NUM),
        f("nCtx", &NULL_NUM),
        f("recipe", &Node::Ref(&ROUTER_SLOT_RECIPE)),
        f("slots", &Node::Array(&Node::Ref(&ROUTER_SLOT))),
        f("error", &NULL_STR),
    ]),
);

pub const ROUTER_SLOTS_RESPONSE: Export = Export::out(
    "RouterSlotsResponse",
    Node::DiscUnion {
        key: "available",
        options: &[
            &obj(&[
                f("available", &AVAILABLE),
                f("sampledAt", &STR),
                f("ageMs", &NUM),
                f("stale", &BOOL),
                f("models", &Node::Array(&Node::Ref(&ROUTER_SLOT_MODEL))),
            ]),
            &Node::Ref(&ROUTER_UNAVAILABLE),
        ],
    },
);

pub const ROUTER_SLOTS_RECORD: Export = Export::out(
    "RouterSlotsRecord",
    obj(&[
        f("kind", &Node::Lit(Lit::Str("slots"))),
        f("ts", &STR),
        f("sampledAt", &STR),
        f("error", &NULL_STR),
        f(
            "models",
            &Node::Array(&obj(&[
                f("model", &STR),
                f("state", &NULL_STR),
                f("ttl", &NULL_NUM),
                f("cmd", &NULL_STR),
                f("slots", &Node::Nullable(&Node::Array(&UNKNOWN_RECORD))),
                f("error", &NULL_STR),
            ])),
        ),
    ]),
);

pub const ROUTER_SIDECAR_RECORD: Export = Export::out(
    "RouterSidecarRecord",
    passthrough(&[
        f("ts", &STR),
        f("kind", &STR),
        f("persona", &STR),
        f("threadId", &NULLISH_STR),
        f("model", &NULLISH_STR),
        f("status", &NULLISH_NUM),
        f("message", &NULLISH_STR),
        f("text", &NULLISH_STR),
    ]),
);

pub const ROUTER_SIDECAR_PERSONA: Export = Export::out(
    "RouterSidecarPersona",
    obj(&[
        f("persona", &STR),
        f("threadId", &NULL_STR),
        f("model", &NULL_STR),
        f("lastSeenTs", &STR),
        f("lastKind", &STR),
    ]),
);

pub const ROUTER_SIDECAR_ERROR: Export = Export::out(
    "RouterSidecarError",
    obj(&[
        f("ts", &STR),
        f("persona", &STR),
        f("kind", &STR),
        f("status", &NULL_NUM),
        f("message", &NULL_STR),
    ]),
);

pub const ROUTER_SIDECAR_RESPONSE: Export = Export::out(
    "RouterSidecarResponse",
    Node::DiscUnion {
        key: "available",
        options: &[
            &obj(&[
                f("available", &AVAILABLE),
                f("generatedAt", &STR),
                f("file", &STR),
                f(
                    "byKind",
                    &Node::Record {
                        key: &STR,
                        value: &NONNEG_INT,
                    },
                ),
                f(
                    "personas",
                    &Node::Array(&Node::Ref(&ROUTER_SIDECAR_PERSONA)),
                ),
                f(
                    "recentErrors",
                    &Node::Array(&Node::Ref(&ROUTER_SIDECAR_ERROR)),
                ),
            ]),
            &Node::Ref(&ROUTER_UNAVAILABLE),
        ],
    },
);
