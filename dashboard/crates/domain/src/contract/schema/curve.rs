//! `curve.ts`.
use super::*;

pub const CURVE_SERIES_POINT: Export = Export::out(
    "CurveSeriesPoint",
    obj(&[
        f("task", &STR),
        f("positionFraction", &NUM),
        f("n", &NONNEG_INT),
        f("k", &NONNEG_INT),
        f("failureRate", &NUM),
        f("sided", &SIDED),
        f("confidence", &NUM),
        f("lower", &NULL_NUM),
        f("upper", &NULL_NUM),
    ]),
);

pub const CURVE_SERIES: Export = Export::out(
    "CurveSeries",
    obj(&[
        f("experimentId", &STR),
        f("points", &Node::Array(&Node::Ref(&CURVE_SERIES_POINT))),
    ]),
);
