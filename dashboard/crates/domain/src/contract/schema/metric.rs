//! `metric.ts`.
use super::*;

pub const METRIC_ROW: Export = Export::out(
    "MetricRow",
    obj(&[
        f("id", &POS_INT),
        f("experimentId", &STR),
        f("metricType", &STR),
        f("roundType", &NULL_STR),
        f("roundIndex", &NULL_INT),
        f("bytes", &NULL_INT),
        f("firstGenerationTokenMs", &NULL_NUM),
        f("observedDecodeTokS", &NULL_NUM),
        f("promptTokens", &NULL_INT),
        f("cachedTokens", &NULL_INT),
        f("cacheHitRatio", &NULL_NUM),
        f("cacheAssertionPassed", &NULL_BOOL),
        f("raw", &UNKNOWN),
    ]),
);
