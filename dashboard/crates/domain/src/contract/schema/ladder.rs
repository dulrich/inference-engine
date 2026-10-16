//! `ladder.ts`.
use super::*;

pub const LADDER_ROW: Export = Export::out(
    "LadderRow",
    obj(&[
        f("experimentId", &STR),
        f("alias", &NULL_STR),
        f("cellId", &NULL_STR),
        f("filename", &NULL_STR),
        f("contextLength", &NULL_INT),
        f("kvCacheTypeK", &NULL_STR),
        f("kvCacheTypeV", &NULL_STR),
        f("flashAttention", &NULL_STR),
        f("medianDecodeTokS", &NULL_NUM),
        f("medianFirstGenerationTokenMs", &NULL_NUM),
        f("correctnessRate", &NULL_NUM),
        f("trialCount", &NONNEG_INT),
    ]),
);
