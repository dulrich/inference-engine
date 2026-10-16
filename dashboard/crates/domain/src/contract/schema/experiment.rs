//! `experiment.ts`, plus `ExperimentFullDetail` (audit B1: the shape `GET /api/experiments/:id`
//! returns, composed today in `apps/web/src/api.ts` as `ExperimentDetailSchema.extend({trials,
//! metrics})`).
use super::*;

pub const LINEAGE: Export = Export::out(
    "Lineage",
    obj(&[
        f("hfRepo", &NULL_STR),
        f("hfRevision", &NULL_STR),
        f("filename", &NULL_STR),
        f("fileSize", &NULL_NONNEG_INT),
        f("sha256", &NULL_STR),
        f("llamaCppBuild", &NULL_STR),
    ]),
);

pub const SWEEP_CELL: Export = Export::out(
    "SweepCell",
    Node::Nullable(&obj(&[
        f("cellId", &STR),
        f("alias", &STR),
        f("serverFlags", &UNKNOWN_RECORD),
        f("command", &NULLISH_STR),
    ])),
);

pub const WILSON_SUMMARY: Export = Export::out(
    "WilsonSummary",
    Node::Nullable(&obj(&[
        f("n", &NONNEG_INT),
        f("k", &NONNEG_INT),
        f("rate", &NUM),
        f("sided", &SIDED),
        f("confidence", &NUM),
        f("lower", &NULL_NUM),
        f("upper", &NULL_NUM),
    ])),
);

/// `ExperimentSummarySchema`'s shape, in declaration order; `.extend` appends after it.
macro_rules! summary_fields {
    ($($more:expr),* $(,)?) => {
        &[
            f("id", &STR),
            f("schemaVersion", &Node::Lit(Lit::Num(2.0))),
            f("description", &NULL_STR),
            f("timestamp", &NULL_STR),
            f("lineage", &Node::Ref(&LINEAGE)),
            f("sweepCell", &Node::Ref(&SWEEP_CELL)),
            f("hasWilsonSummary", &BOOL),
            f("samplingArm", &NULL_STR),
            f("battery", &Node::Nullable(&STR_LIST)),
            f("vramProfile", &NULL_STR),
            f("trialCount", &NONNEG_INT),
            f("metricCount", &NONNEG_INT),
            $($more),*
        ]
    };
}

pub const EXPERIMENT_SUMMARY: Export = Export::out("ExperimentSummary", obj(summary_fields!()));

pub const EXPERIMENT_DETAIL: Export = Export::out(
    "ExperimentDetail",
    obj(summary_fields!(
        f("wilsonSummary", &Node::Ref(&WILSON_SUMMARY)),
        f("responseCaps", &Node::Nullable(&UNKNOWN_RECORD)),
    )),
);

pub const EXPERIMENT_FULL_DETAIL: Export = Export::out(
    "ExperimentFullDetail",
    obj(summary_fields!(
        f("wilsonSummary", &Node::Ref(&WILSON_SUMMARY)),
        f("responseCaps", &Node::Nullable(&UNKNOWN_RECORD)),
        f("trials", &Node::Array(&Node::Ref(&TRIAL_ROW))),
        f("metrics", &Node::Array(&Node::Ref(&METRIC_ROW))),
    )),
);
