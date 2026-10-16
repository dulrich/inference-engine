//! `serving.ts`.
use super::*;

pub const SERVING_FLAG_ENTRY: Export = Export::out(
    "ServingFlagEntry",
    obj(&[f("flag", &STR), f("value", &NULL_STR)]),
);

pub const SERVING_FLAGS: Export = Export::out(
    "ServingFlags",
    obj(&[
        f("contextLength", &NULL_INT),
        f("kvCacheTypeK", &NULL_STR),
        f("kvCacheTypeV", &NULL_STR),
        f("flashAttention", &NULL_STR),
        f("gpuLayers", &NULL_INT),
        f(
            "allFlags",
            &Node::Default(
                &Node::Array(&Node::Ref(&SERVING_FLAG_ENTRY)),
                DefaultValue::EmptyArray,
            ),
        ),
    ]),
);

/// `verified` is a bare `YYYY-MM-DD` date.
pub const SERVING_VERIFIED_DATE_PATTERN: Pattern = Pattern {
    name: "SERVING_VERIFIED_DATE_PATTERN",
    source: r"^\d{4}-\d{2}-\d{2}$",
};

const VERIFIED: Node = Node::Str(StrRules::ANY.regex(&SERVING_VERIFIED_DATE_PATTERN));

pub const SERVING_SOURCE: Export = Export::out(
    "ServingSource",
    Node::Union(&[
        &obj(&[
            f("url", &Node::Lit(Lit::Str("none"))),
            f("reason", &STR),
            f("verified", &Node::Optional(&Node::Nullable(&VERIFIED))),
        ]),
        &obj(&[
            f("url", &STR),
            f("verified", &VERIFIED),
            f("reason", &NULLISH_STR),
        ]),
    ]),
);

const NULL_NUM_D: Node = Node::Default(&NULL_NUM, DefaultValue::Null);
const NULL_INT_D: Node = Node::Default(&NULL_INT, DefaultValue::Null);
const NULL_STR_D: Node = Node::Default(&NULL_STR, DefaultValue::Null);
const STR_LIST_D: Node = Node::Default(&STR_LIST, DefaultValue::EmptyArray);

pub const SERVING_CAPABILITY: Export = Export::out(
    "ServingCapability",
    obj(&[
        f("contextLength", &NULL_INT),
        f("inputModalities", &STR_LIST_D),
        f("outputModalities", &STR_LIST_D),
        f("toolCalling", &NULL_BOOL),
        f("reasoning", &NULL_STR),
        f(
            "serving",
            &obj(&[
                f("quant", &NULL_STR),
                f("kvCacheType", &NULL_STR),
                f("flashAttention", &NULL_BOOL),
                f("mtp", &NULL_BOOL),
                f("moeCpuOffloadLayers", &NULL_INT_D),
                f("ubatch", &NULL_INT_D),
                f("reasoningMode", &NULL_STR_D),
                f("reasoningBudget", &NULL_INT_D),
            ]),
        ),
        f(
            "sampling",
            &Node::Default(
                &Node::Nullable(&obj(&[
                    f("temperature", &NULL_NUM_D),
                    f("topP", &NULL_NUM_D),
                    f("topK", &NULL_INT_D),
                    f("minP", &NULL_NUM_D),
                    f("presencePenalty", &NULL_NUM_D),
                    f("repetitionPenalty", &NULL_NUM_D),
                ])),
                DefaultValue::Null,
            ),
        ),
        f(
            "maxOutput",
            &Node::Default(
                &Node::Nullable(&obj(&[
                    f("reasoningBudget", &NULL_INT_D),
                    f("answerTokens", &NULL_INT_D),
                ])),
                DefaultValue::Null,
            ),
        ),
        f(
            "sources",
            &Node::Default(
                &Node::Nullable(&Node::Record {
                    key: &STR,
                    value: &Node::Ref(&SERVING_SOURCE),
                }),
                DefaultValue::Null,
            ),
        ),
    ]),
);

pub const SERVING_RECIPE: Export = Export::out(
    "ServingRecipe",
    obj(&[
        f("modelPath", &NULL_STR),
        f("draftPath", &NULL_STR),
        f("specType", &NULL_STR),
        f("moeCpuLayers", &NULL_INT),
        f("kvK", &STR),
        f("kvV", &STR),
    ]),
);

pub const SERVING_ALIAS: Export = Export::out(
    "ServingAlias",
    obj(&[
        f("modelKey", &STR),
        f("name", &NULL_STR),
        f("aliases", &STR_LIST),
        f("ttl", &NULL_INT),
        f("checkEndpoint", &NULL_STR),
        f("flags", &Node::Ref(&SERVING_FLAGS)),
        f(
            "capability",
            &Node::Default(
                &Node::Nullable(&Node::Ref(&SERVING_CAPABILITY)),
                DefaultValue::Null,
            ),
        ),
        f(
            "recipe",
            &Node::Default(
                &Node::Nullable(&Node::Ref(&SERVING_RECIPE)),
                DefaultValue::Null,
            ),
        ),
    ]),
);

pub const SERVING_RESPONSE: Export = Export::out(
    "ServingResponse",
    obj(&[
        f("configPath", &STR),
        f("models", &Node::Array(&Node::Ref(&SERVING_ALIAS))),
    ]),
);

pub const SERVING_TIMING_ROW: Export = Export::out(
    "ServingTimingRow",
    obj(&[
        f("modelKey", &STR),
        f("aliases", &STR_LIST),
        f("p50PrefillTokS", &NULL_NUM),
        f("p90PrefillTokS", &NULL_NUM),
        f("p50DecodeTokS", &NULL_NUM),
        f("p90DecodeTokS", &NULL_NUM),
        f("samplingArm", &NULL_STR),
        f("sampleCount", &INT),
        f("mtp", &BOOL),
    ]),
);
