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

/// The retired `router/visibility.json`'s schema literal (WP-3 moved visibility into the model
/// catalog, `router/catalog/catalog.json`); kept for the contract-v1 fixtures.
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

// ------------------------------------------------------------------------------------------
// `serving-request-v2` (plans/serving-control-redesign.md WP-4): the catalog intents. The
// dispatcher (`control/serving_protocol.py` `_validate_v2`, `control/catalog_intents.py`) owns
// every semantic rule (provider exists, duplicate/protected id, alias lookup); this side checks
// shape only and writes the drop-box file. Wire field names are the dispatcher's.
// ------------------------------------------------------------------------------------------

/// `serving_protocol.SCHEMA_REQUEST_V2`.
pub const SERVING_REQUEST_SCHEMA_V2: &str = "serving-request-v2";

/// `catalog_intents.PROVIDER_RE`.
pub const SERVING_CATALOG_PROVIDER_PATTERN: Pattern = Pattern {
    name: "SERVING_CATALOG_PROVIDER_PATTERN",
    source: "^[a-z0-9][a-z0-9-]{0,63}$",
};

/// `catalog_intents.CATALOG_ID_RE`: a plain upstream slug or `@preset/<slug>`.
pub const SERVING_CATALOG_ID_PATTERN: Pattern = Pattern {
    name: "SERVING_CATALOG_ID_PATTERN",
    // `/` is escaped: the source is emitted verbatim as a JS regex literal.
    source: "^(?:@preset\\/[a-z0-9][a-z0-9._-]{0,126}|[A-Za-z0-9][A-Za-z0-9._:\\/+-]{0,199})$",
};

/// `serving_protocol.MODEL_KEY_RE` (the v2 `catalog.consumers` target).
pub const SERVING_CATALOG_MODEL_KEY_PATTERN: Pattern = Pattern {
    name: "SERVING_CATALOG_MODEL_KEY_PATTERN",
    source: "^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
};

const CATALOG_PROVIDER: Node = Node::Str(StrRules::ANY.regex_msg(
    &SERVING_CATALOG_PROVIDER_PATTERN,
    "provider must be a gateway provider name",
));
const CATALOG_ID: Node = Node::Str(StrRules::ANY.regex_msg(
    &SERVING_CATALOG_ID_PATTERN,
    "id must be a model slug or @preset/<slug>",
));
const CATALOG_MODEL_KEY: Node = Node::Str(StrRules::ANY.regex_msg(
    &SERVING_CATALOG_MODEL_KEY_PATTERN,
    "modelKey must be a llama/config.yaml model key",
));

pub const SERVING_CATALOG_ACTION_VALUES: &[&str] =
    &["catalog.add", "catalog.remove", "catalog.consumers"];
pub const SERVING_CATALOG_ACTION: Export = Export::out(
    "ServingCatalogAction",
    Node::Enum(SERVING_CATALOG_ACTION_VALUES),
);

/// `POST /api/catalog/add`.
pub const SERVING_CATALOG_ADD_REQUEST: Export = Export::out(
    "ServingCatalogAddRequest",
    obj(&[
        f("provider", &CATALOG_PROVIDER),
        f("id", &CATALOG_ID),
        f("consumers", &Node::Optional(&CONSUMERS)),
    ]),
);

/// `POST /api/catalog/remove`.
pub const SERVING_CATALOG_REMOVE_REQUEST: Export = Export::out(
    "ServingCatalogRemoveRequest",
    obj(&[f("provider", &CATALOG_PROVIDER), f("id", &CATALOG_ID)]),
);

/// `POST /api/catalog/consumers`: `provider` + `id`, or `modelKey` alone (the server enforces
/// the either/or; an exposure record has only a model key).
pub const SERVING_CATALOG_CONSUMERS_REQUEST: Export = Export::out(
    "ServingCatalogConsumersRequest",
    obj(&[
        f("provider", &Node::Optional(&CATALOG_PROVIDER)),
        f("id", &Node::Optional(&CATALOG_ID)),
        f("modelKey", &Node::Optional(&CATALOG_MODEL_KEY)),
        f("consumers", &CONSUMERS),
    ]),
);

/// The drop-box file (`serving-request-v2`), as the dispatcher reads it.
pub const SERVING_CATALOG_REQUEST: Export = Export::out(
    "ServingCatalogRequest",
    obj(&[
        f("schema", &Node::Lit(Lit::Str(SERVING_REQUEST_SCHEMA_V2))),
        f(
            "request_id",
            &Node::Str(StrRules::ANY.regex_msg(
                &SERVING_REQUEST_ID_PATTERN,
                "request_id must match the serving request-id grammar",
            )),
        ),
        f("action", &Node::Ref(&SERVING_CATALOG_ACTION)),
        f("provider", &Node::Optional(&CATALOG_PROVIDER)),
        f("id", &Node::Optional(&CATALOG_ID)),
        f("model_key", &Node::Optional(&CATALOG_MODEL_KEY)),
        f("consumers", &Node::Optional(&CONSUMERS)),
        f("requested_at", &STR_MIN1),
    ]),
);

/// `catalog_intents.catalog_projection`: one catalog entry as the panel sees it. `consumers` is
/// the raw list (it may carry the router-internal `router` tag) or `null` when the entry carries
/// none (the picker reads absent as "every consumer").
pub const SERVING_CATALOG_MODEL: Export = Export::out(
    "ServingCatalogModel",
    obj(&[
        f("id", &STR),
        f("model_key", &NULL_STR),
        f("consumers", &Node::Nullable(&STR_LIST)),
        f("protected", &BOOL),
    ]),
);

pub const SERVING_CATALOG_PROVIDER_KIND_VALUES: &[&str] = &["local", "remote", "codex"];
pub const SERVING_CATALOG_PROVIDER_KIND: Export = Export::out(
    "ServingCatalogProviderKind",
    Node::Enum(SERVING_CATALOG_PROVIDER_KIND_VALUES),
);

pub const SERVING_CATALOG_PROVIDER: Export = Export::out(
    "ServingCatalogProvider",
    obj(&[
        f("name", &STR),
        f("kind", &Node::Ref(&SERVING_CATALOG_PROVIDER_KIND)),
        f("presets", &BOOL),
        f("models", &Node::Array(&Node::Ref(&SERVING_CATALOG_MODEL))),
    ]),
);

pub const SERVING_CATALOG_EXPOSURE: Export = Export::out(
    "ServingCatalogExposure",
    obj(&[f("model_key", &STR), f("consumers", &STR_LIST)]),
);

/// `serving-status-v1`'s additive `catalog` block.
pub const SERVING_CATALOG_PROJECTION: Export = Export::out(
    "ServingCatalogProjection",
    obj(&[
        f("revision", &STR),
        f(
            "providers",
            &Node::Array(&Node::Ref(&SERVING_CATALOG_PROVIDER)),
        ),
        f(
            "exposures",
            &Node::Array(&Node::Ref(&SERVING_CATALOG_EXPOSURE)),
        ),
        f("addable_local_aliases", &STR_LIST),
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
        // WP-4, additive: the catalog revision a v2 intent left live (absent on v1 records).
        f("revision", &NULLISH_STR),
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
            // WP-4, additive: absent until the dispatcher publishes it.
            f("catalog", &Node::Optional(&Node::Ref(&SERVING_CATALOG_PROJECTION))),
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
