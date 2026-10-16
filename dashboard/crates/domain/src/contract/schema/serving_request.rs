//! `servingRequest.ts`.
use super::*;

pub const SERVING_CONSUMER_VALUES: &[&str] = &["cc", "openwebui"];
pub const SERVING_CONSUMER: Export =
    Export::out("ServingConsumer", Node::Enum(SERVING_CONSUMER_VALUES));

/// The canonical `consumers` order.
pub const SERVING_CONSUMER_ORDER: &[&str] = &["cc", "openwebui"];

const CONSUMERS: Node = Node::Array(&Node::Ref(&SERVING_CONSUMER));

pub const SERVING_VISIBILITY_REQUEST: Export = Export::out(
    "ServingVisibilityRequest",
    obj(&[
        f(
            "modelKey",
            &Node::Str(StrRules::ANY.min_msg(
                1,
                "modelKey must be a non-empty llama/config.yaml model key",
            )),
        ),
        f("consumers", &CONSUMERS),
    ]),
);

/// `serving_protocol.SCHEMA_REQUEST`.
pub const SERVING_REQUEST_SCHEMA_V1: &str = "serving-request-v1";

/// `serving_protocol.REQUEST_BASENAME_RE`, minus the `.json` suffix.
pub const SERVING_REQUEST_ID_PATTERN: Pattern = Pattern {
    name: "SERVING_REQUEST_ID_PATTERN",
    source: "^srv-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$",
};

pub const SERVING_ACTION_VALUES: &[&str] = &["set-visibility"];
pub const SERVING_ACTION: Export = Export::out("ServingAction", Node::Enum(SERVING_ACTION_VALUES));

pub const SERVING_REQUEST: Export = Export::out(
    "ServingRequest",
    obj(&[
        f("schema", &Node::Lit(Lit::Str(SERVING_REQUEST_SCHEMA_V1))),
        f(
            "request_id",
            &Node::Str(StrRules::ANY.regex_msg(
                &SERVING_REQUEST_ID_PATTERN,
                "request_id must match the serving request-id grammar",
            )),
        ),
        f("action", &Node::Ref(&SERVING_ACTION)),
        f("model_key", &STR_MIN1),
        f("consumers", &CONSUMERS),
        f("requested_at", &STR_MIN1),
    ]),
);

pub const SERVING_ACCEPTED_RESPONSE: Export = Export::out(
    "ServingAcceptedResponse",
    obj(&[
        f("requestId", &STR),
        f("accepted", &Node::Lit(Lit::Bool(true))),
        f("message", &STR),
    ]),
);

/// `serving_protocol.SCHEMA_STATUS`.
pub const SERVING_STATUS_SCHEMA_V1: &str = "serving-status-v1";

pub const SERVING_VISIBILITY_ENTRY: Export = Export::out(
    "ServingVisibilityEntry",
    obj(&[f("gateway_id", &NULL_STR), f("consumers", &CONSUMERS)]),
);

pub const SERVING_VISIBILITY_MAP: Export = Export::out(
    "ServingVisibilityMap",
    Node::Record {
        key: &STR,
        value: &Node::Ref(&SERVING_VISIBILITY_ENTRY),
    },
);

/// `router/visibility.json`'s own schema literal.
pub const SERVING_VISIBILITY_SCHEMA_V1: &str = "serving-visibility-v1";

pub const SERVING_VISIBILITY_DECLARATION: Export = Export::out(
    "ServingVisibilityDeclaration",
    obj(&[
        f("schema", &Node::Lit(Lit::Str(SERVING_VISIBILITY_SCHEMA_V1))),
        f("models", &Node::Ref(&SERVING_VISIBILITY_MAP)),
    ]),
);

/// `ServingVisibilityDeclarationSchema.extend({ models: z.record(z.string(), z.unknown()) })`:
/// `models` keeps its slot.
pub const SERVING_VISIBILITY_ENVELOPE: Export = Export::out(
    "ServingVisibilityEnvelope",
    obj(&[
        f("schema", &Node::Lit(Lit::Str(SERVING_VISIBILITY_SCHEMA_V1))),
        f("models", &UNKNOWN_RECORD),
    ]),
);

pub const SERVING_REQUEST_STATE_VALUES: &[&str] = &["applied", "rejected", "failed"];
pub const SERVING_REQUEST_STATE: Export = Export::out(
    "ServingRequestState",
    Node::Enum(SERVING_REQUEST_STATE_VALUES),
);

pub const SERVING_REQUEST_RECORD: Export = Export::out(
    "ServingRequestRecord",
    obj(&[
        f("request_id", &STR),
        f("state", &Node::Ref(&SERVING_REQUEST_STATE)),
        f("reason", &Node::Default(&NULL_STR, DefaultValue::Null)),
        f("applied_at", &STR),
    ]),
);

/// `ServingStatusFileSchema`'s shape; `.extend` appends after it.
macro_rules! status_file_fields {
    ($($more:expr),* $(,)?) => {
        &[
            f("schema", &Node::Lit(Lit::Str(SERVING_STATUS_SCHEMA_V1))),
            f("generated_at", &STR_MIN1),
            f("visibility", &Node::Ref(&SERVING_VISIBILITY_MAP)),
            f("last_request", &Node::Nullable(&Node::Ref(&SERVING_REQUEST_RECORD))),
            f("recent", &Node::Array(&Node::Ref(&SERVING_REQUEST_RECORD))),
            $($more),*
        ]
    };
}

pub const SERVING_STATUS_FILE: Export =
    Export::out("ServingStatusFile", obj(status_file_fields!()));

pub const SERVING_STATUS_UNAVAILABLE_REASON_VALUES: &[&str] =
    &["disabled", "unreadable", "empty", "stale"];
pub const SERVING_STATUS_UNAVAILABLE_REASON: Export = Export::out(
    "ServingStatusUnavailableReason",
    Node::Enum(SERVING_STATUS_UNAVAILABLE_REASON_VALUES),
);

/// 180 s, the queue control plane's own offline threshold.
pub const SERVING_STATUS_STALE_MS: i64 = 180_000;

pub const SERVING_STATUS_RESPONSE: Export = Export::out(
    "ServingStatusResponse",
    Node::Union(&[
        &obj(&[
            f("available", &Node::Lit(Lit::Bool(false))),
            f("reason", &Node::Ref(&SERVING_STATUS_UNAVAILABLE_REASON)),
        ]),
        &obj(status_file_fields!(f(
            "available",
            &Node::Lit(Lit::Bool(true))
        ))),
    ]),
);
