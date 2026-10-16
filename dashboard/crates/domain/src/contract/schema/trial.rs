//! `trial.ts`.
use super::*;

pub const TRIAL_ROW: Export = Export::out(
    "TrialRow",
    obj(&[
        f("id", &POS_INT),
        f("experimentId", &STR),
        f("lineIndex", &NONNEG_INT),
        f("type", &STR),
        f("trial", &NULL_NONNEG_INT),
        f("samplingArm", &NULL_STR),
        f("temperature", &NULL_NUM),
        f("topP", &NULL_NUM),
        f("topK", &NULL_INT),
        f("samplerSeed", &NULL_NUM),
        f("haystackSeed", &NULL_NUM),
        f("corpusManifestId", &NULL_STR),
        f("haystackSha256", &NULL_STR),
        f("classifierVerdict", &NULL_STR),
        f("classifierShape", &NULL_STR),
        f("promptTokens", &NULL_INT),
        f("completionTokens", &NULL_INT),
        f("finishReason", &NULL_STR),
        f("positionFraction", &NULL_NUM),
        f("task", &NULL_STR),
        f("cellId", &NULL_STR),
        f("correct", &NULL_BOOL),
        f("raw", &UNKNOWN),
    ]),
);
