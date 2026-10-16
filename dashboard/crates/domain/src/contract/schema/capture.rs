//! `capture.ts`.
use super::*;

pub const CAPTURE_SERVING: Export = Export::out(
    "CaptureServing",
    obj(&[
        f("modelKey", &STR),
        f("aliases", &STR_LIST),
        f("recipeMatch", &BOOL),
        f("mtp", &BOOL),
        f("moeCpuLayers", &NULL_INT),
        f("specType", &NULL_STR),
    ]),
);

pub const CAPTURE_RECORD: Export = Export::out(
    "CaptureRecord",
    obj(&[
        f("experimentId", &STR),
        f("config", &UNKNOWN),
        f("prompts", &UNKNOWN_LIST),
        f("completions", &UNKNOWN_LIST),
        f("samples", &UNKNOWN_LIST),
        f(
            "serving",
            &Node::Default(
                &Node::Nullable(&Node::Ref(&CAPTURE_SERVING)),
                DefaultValue::Null,
            ),
        ),
    ]),
);

pub const CAPTURE_ENVELOPE: Export = Export::out(
    "CaptureEnvelope",
    obj(&[
        f("captureVersion", &Node::Lit(Lit::Num(1.0))),
        f("stability", &Node::Lit(Lit::Str("v1"))),
        f("generatedAt", &STR),
        f("recordCount", &NONNEG_INT),
        f("records", &Node::Array(&Node::Ref(&CAPTURE_RECORD))),
    ]),
);
