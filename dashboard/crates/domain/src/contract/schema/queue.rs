//! `queue.ts`.
use super::*;

pub const CELL_STATE_VALUES: &[&str] = &[
    "pending",
    "running",
    "completed",
    "failed",
    "skipped-propagated",
    "aborted-hang",
];
pub const CELL_STATE: Export = Export::out("CellState", Node::Enum(CELL_STATE_VALUES));

pub const QUEUE_STATE_VALUES: &[&str] =
    &["running", "paused-resumable", "completed", "aborted-hang"];
pub const QUEUE_STATE: Export = Export::out("QueueState", Node::Enum(QUEUE_STATE_VALUES));

pub const QUEUE_CELL_COUNTS: Export = Export::out(
    "QueueCellCounts",
    obj(&[
        f("pending", &NULL_NONNEG_INT),
        f("running", &NULL_NONNEG_INT),
        f("completed", &NULL_NONNEG_INT),
        f("failed", &NULL_NONNEG_INT),
        f("skipped-propagated", &NULL_NONNEG_INT),
        f("aborted-hang", &NULL_NONNEG_INT),
    ]),
);

pub const QUEUE_CELL: Export = Export::out(
    "QueueCell",
    obj(&[
        f("alias", &STR),
        f("model", &NULL_STR),
        f("c", &NULL_INT),
        f("ctk", &NULL_STR),
        f("ctv", &NULL_STR),
        f("fa", &NULL_STR),
        f("samplingArm", &NULL_STR),
        f("trials", &NULL_INT),
        f("state", &Node::Nullable(&Node::Ref(&CELL_STATE))),
        f("reason", &NULL_STR),
    ]),
);

pub const QUEUE_ETA: Export = Export::out(
    "QueueEta",
    obj(&[
        f("remainingCells", &NULL_NONNEG_INT),
        f("medianCellMinutes", &NULL_NUM),
        f("estimatedMinutesRemaining", &NULL_NUM),
        f("cellsPerHour", &NULL_NUM),
        f(
            "basis",
            &Node::Nullable(&Node::Enum(&["current-run", "prior-runs", "model-table"])),
        ),
        f("sampleSize", &NULL_NONNEG_INT),
        f("bucketSampleSize", &Node::Optional(&NULL_NONNEG_INT)),
        f("scalingCaveat", &Node::Optional(&NULL_BOOL)),
    ]),
);

pub const QUEUE_ENTRY: Export = Export::out(
    "QueueEntry",
    obj(&[
        f("name", &STR),
        f("queueState", &Node::Nullable(&Node::Ref(&QUEUE_STATE))),
        f("deadline", &NULL_STR),
        f("vramProfile", &NULL_STR),
        f("cellCounts", &Node::Ref(&QUEUE_CELL_COUNTS)),
        f("cells", &Node::Array(&Node::Ref(&QUEUE_CELL))),
        f("eta", &Node::Optional(&Node::Ref(&QUEUE_ETA))),
    ]),
);

pub const QUEUE_ERROR_ENTRY: Export = Export::out(
    "QueueErrorEntry",
    obj(&[
        f("name", &STR),
        f("error", &STR),
        f("eta", &Node::Optional(&Node::Null)),
    ]),
);

pub const QUEUE_LIST_ITEM: Export = Export::out(
    "QueueListItem",
    Node::Union(&[&Node::Ref(&QUEUE_ENTRY), &Node::Ref(&QUEUE_ERROR_ENTRY)]),
);

pub const QUEUE_LIST_RESPONSE: Export = Export::out(
    "QueueListResponse",
    obj(&[
        f("queues", &Node::Array(&Node::Ref(&QUEUE_LIST_ITEM))),
        f("generatedAt", &STR),
        f(
            "control",
            &Node::Optional(&Node::Nullable(&Node::Ref(&CONTROL_SECTION))),
        ),
    ]),
);
