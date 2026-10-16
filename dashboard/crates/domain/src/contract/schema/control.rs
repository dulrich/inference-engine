//! `control.ts`.
use super::*;

pub const CONTROL_REQUEST_STATE_VALUES: &[&str] = &[
    "pending",
    "claimed",
    "running",
    "done",
    "rejected",
    "canceled",
    "launch-failed",
];
pub const CONTROL_REQUEST_STATE: Export = Export::out(
    "ControlRequestState",
    Node::Enum(CONTROL_REQUEST_STATE_VALUES),
);

pub const CONTROL_LAST_RUN_DISPOSITION_VALUES: &[&str] = &["clean", "needs-attention", "crashed"];
pub const CONTROL_LAST_RUN_DISPOSITION: Export = Export::out(
    "ControlLastRunDisposition",
    Node::Enum(CONTROL_LAST_RUN_DISPOSITION_VALUES),
);

pub const CONTROL_ACTION_VALUES: &[&str] = &["arm", "disarm", "start", "stop", "cancel"];
pub const CONTROL_ACTION: Export = Export::out("ControlAction", Node::Enum(CONTROL_ACTION_VALUES));

pub const CONTROL_LIVE: Export = Export::out(
    "ControlLive",
    obj(&[
        f("requestId", &STR),
        f("queueStem", &STR),
        f("phase", &STR),
        f("startedAt", &STR),
    ]),
);

pub const CONTROL_LAST_RUN: Export = Export::out(
    "ControlLastRun",
    obj(&[
        f("requestId", &STR),
        f("endedAt", &STR),
        f("runExit", &NULL_INT),
        f("verifyExit", &NULL_INT),
        f("restartFailed", &NULL_STR),
        f("disposition", &Node::Ref(&CONTROL_LAST_RUN_DISPOSITION)),
    ]),
);

pub const CONTROL_REQUEST_DIGEST: Export = Export::out(
    "ControlRequestDigest",
    obj(&[
        f("requestId", &STR),
        f("action", &Node::Ref(&CONTROL_ACTION)),
        f("state", &Node::Ref(&CONTROL_REQUEST_STATE)),
        f("reason", &NULL_STR),
        f("requestedAt", &STR),
    ]),
);

pub const CONTROL_SECTION: Export = Export::out(
    "ControlSection",
    obj(&[
        f("statusGeneratedAt", &STR),
        f("stale", &BOOL),
        f("armed", &BOOL),
        f("live", &Node::Nullable(&Node::Ref(&CONTROL_LIVE))),
        f("lastRun", &Node::Nullable(&Node::Ref(&CONTROL_LAST_RUN))),
        f("anomalies", &STR_LIST),
        f(
            "requests",
            &Node::Array(&Node::Ref(&CONTROL_REQUEST_DIGEST)),
        ),
    ]),
);

/// `control_protocol.QUEUE_STEM_RE`.
pub const CONTROL_QUEUE_STEM_PATTERN: Pattern = Pattern {
    name: "CONTROL_QUEUE_STEM_PATTERN",
    source: "^[a-z0-9][a-z0-9-]*$",
};
/// `control_protocol.REQUEST_BASENAME_RE`, minus the `.json` suffix.
pub const CONTROL_REQUEST_ID_PATTERN: Pattern = Pattern {
    name: "CONTROL_REQUEST_ID_PATTERN",
    source: "^req-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$",
};
/// `control_protocol.MINUTES_MIN` / `MINUTES_MAX`.
pub const CONTROL_MINUTES_MIN: i64 = 15;
pub const CONTROL_MINUTES_MAX: i64 = 480;
/// `control_protocol.SCHEMA_REQUEST`.
pub const CONTROL_REQUEST_SCHEMA_V1: &str = "control-request-v1";
/// `control_protocol.HOST_ID_RE`.
pub const CONTROL_TARGET_ID_PATTERN: Pattern = Pattern {
    name: "CONTROL_TARGET_ID_PATTERN",
    source: "^(req-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}|nightly-[0-9]{8})$",
};

const MINUTES: Node = Node::Num(
    NumRules::ANY
        .int()
        .gte(CONTROL_MINUTES_MIN as f64)
        .lte(CONTROL_MINUTES_MAX as f64),
);

pub const CONTROL_START_REQUEST: Export = Export::out(
    "ControlStartRequest",
    obj(&[
        f(
            "queueStem",
            &Node::Str(StrRules::ANY.regex_msg(
                &CONTROL_QUEUE_STEM_PATTERN,
                "queueStem must match ^[a-z0-9][a-z0-9-]*$",
            )),
        ),
        f("minutes", &MINUTES),
    ]),
);

pub const CONTROL_CANCEL_REQUEST: Export = Export::out(
    "ControlCancelRequest",
    obj(&[f(
        "targetRequestId",
        &Node::Str(StrRules::ANY.regex_msg(
            &CONTROL_REQUEST_ID_PATTERN,
            "targetRequestId must match the request-id grammar",
        )),
    )]),
);

pub const CONTROL_REQUEST_FILE: Export = Export::out(
    "ControlRequestFile",
    obj(&[
        f("schema", &Node::Lit(Lit::Str(CONTROL_REQUEST_SCHEMA_V1))),
        f(
            "request_id",
            &Node::Str(StrRules::ANY.regex_msg(
                &CONTROL_REQUEST_ID_PATTERN,
                "request_id must match the request-id grammar",
            )),
        ),
        f("action", &Node::Ref(&CONTROL_ACTION)),
        f(
            "queue_stem",
            &Node::Optional(&Node::Str(StrRules::ANY.regex_msg(
                &CONTROL_QUEUE_STEM_PATTERN,
                "queue_stem must match ^[a-z0-9][a-z0-9-]*$",
            ))),
        ),
        f("minutes", &Node::Optional(&MINUTES)),
        f(
            "target_request_id",
            &Node::Optional(&Node::Str(StrRules::ANY.regex_msg(
                &CONTROL_TARGET_ID_PATTERN,
                "target_request_id must match the host-id grammar",
            ))),
        ),
        f("requested_at", &STR_MIN1),
    ]),
);

pub const CONTROL_ACCEPTED_RESPONSE: Export = Export::out(
    "ControlAcceptedResponse",
    obj(&[
        f("requestId", &STR),
        f("accepted", &Node::Lit(Lit::Bool(true))),
        f("message", &Node::Optional(&STR)),
    ]),
);
