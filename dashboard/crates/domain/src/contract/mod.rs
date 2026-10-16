//! The `@bench-dashboard/shared` contract as data (plan Decision 5). One tree drives the Rust
//! validator (`validate`) and the generated TS runtime validators (`tsgen`). zod v3 semantics:
//! unknown object keys stripped (or kept, for `.passthrough()`), object output in declared field
//! order, valid/dirty/aborted results. Copied from radar_tracker; the schemas themselves live in
//! `schema/`, one module per `packages/shared/src/*.ts` file.
mod node;
mod schema;

pub use node::*;
pub use schema::*;

/// Every schema, in dependency order (each `Ref` target precedes its first user).
pub const EXPORTS: &[&Export] = &[
    // capture.ts
    &CAPTURE_SERVING,
    &CAPTURE_RECORD,
    &CAPTURE_ENVELOPE,
    // control.ts
    &CONTROL_REQUEST_STATE,
    &CONTROL_LAST_RUN_DISPOSITION,
    &CONTROL_ACTION,
    &CONTROL_LIVE,
    &CONTROL_LAST_RUN,
    &CONTROL_REQUEST_DIGEST,
    &CONTROL_SECTION,
    &CONTROL_START_REQUEST,
    &CONTROL_CANCEL_REQUEST,
    &CONTROL_REQUEST_FILE,
    &CONTROL_ACCEPTED_RESPONSE,
    // curve.ts
    &CURVE_SERIES_POINT,
    &CURVE_SERIES,
    // trial.ts, metric.ts (before experiment.ts: `ExperimentFullDetail` uses both)
    &TRIAL_ROW,
    &METRIC_ROW,
    // experiment.ts
    &LINEAGE,
    &SWEEP_CELL,
    &WILSON_SUMMARY,
    &EXPERIMENT_SUMMARY,
    &EXPERIMENT_DETAIL,
    &EXPERIMENT_FULL_DETAIL,
    // health.ts, ingestSummary.ts, ladder.ts
    &HEALTH_RESPONSE,
    &INGEST_SUMMARY,
    &LADDER_ROW,
    // queue.ts
    &CELL_STATE,
    &QUEUE_STATE,
    &QUEUE_CELL_COUNTS,
    &QUEUE_CELL,
    &QUEUE_ETA,
    &QUEUE_ENTRY,
    &QUEUE_ERROR_ENTRY,
    &QUEUE_LIST_ITEM,
    &QUEUE_LIST_RESPONSE,
    // routerStatus.ts
    &ROUTER_UNAVAILABLE_REASON,
    &ROUTER_UNAVAILABLE,
    &ROUTER_UPSTREAM_TIMINGS,
    &ROUTER_REQUEST_RECORD,
    &ROUTER_PROGRESS_RECORD,
    &ROUTER_IN_FLIGHT,
    &ROUTER_LEG_SUMMARY,
    &ROUTER_TELEMETRY_RESPONSE,
    &ROUTER_SLOT,
    &ROUTER_SLOT_RECIPE,
    &ROUTER_SLOT_MODEL,
    &ROUTER_SLOTS_RESPONSE,
    &ROUTER_SLOTS_RECORD,
    &ROUTER_SIDECAR_RECORD,
    &ROUTER_SIDECAR_PERSONA,
    &ROUTER_SIDECAR_ERROR,
    &ROUTER_SIDECAR_RESPONSE,
    // serving.ts
    &SERVING_FLAG_ENTRY,
    &SERVING_FLAGS,
    &SERVING_SOURCE,
    &SERVING_CAPABILITY,
    &SERVING_RECIPE,
    &SERVING_ALIAS,
    &SERVING_RESPONSE,
    &SERVING_TIMING_ROW,
    // servingRequest.ts
    &SERVING_CONSUMER,
    &SERVING_VISIBILITY_REQUEST,
    &SERVING_ACTION,
    &SERVING_REQUEST,
    &SERVING_ACCEPTED_RESPONSE,
    &SERVING_VISIBILITY_ENTRY,
    &SERVING_VISIBILITY_MAP,
    &SERVING_VISIBILITY_DECLARATION,
    &SERVING_VISIBILITY_ENVELOPE,
    &SERVING_REQUEST_STATE,
    &SERVING_REQUEST_RECORD,
    &SERVING_STATUS_FILE,
    &SERVING_STATUS_UNAVAILABLE_REASON,
    &SERVING_STATUS_RESPONSE,
];

/// Every non-schema `export const`, in source order; `gen-ts` emits them verbatim before the
/// schemas (the regex constants are referenced by the string nodes).
pub const CONSTS: &[Const] = &[
    Const {
        name: "CONTROL_QUEUE_STEM_PATTERN",
        value: ConstValue::Regex(&CONTROL_QUEUE_STEM_PATTERN),
    },
    Const {
        name: "CONTROL_REQUEST_ID_PATTERN",
        value: ConstValue::Regex(&CONTROL_REQUEST_ID_PATTERN),
    },
    Const {
        name: "CONTROL_MINUTES_MIN",
        value: ConstValue::Int(CONTROL_MINUTES_MIN),
    },
    Const {
        name: "CONTROL_MINUTES_MAX",
        value: ConstValue::Int(CONTROL_MINUTES_MAX),
    },
    Const {
        name: "CONTROL_REQUEST_SCHEMA_V1",
        value: ConstValue::Str(CONTROL_REQUEST_SCHEMA_V1),
    },
    Const {
        name: "CONTROL_TARGET_ID_PATTERN",
        value: ConstValue::Regex(&CONTROL_TARGET_ID_PATTERN),
    },
    Const {
        name: "SERVING_VERIFIED_DATE_PATTERN",
        value: ConstValue::Regex(&SERVING_VERIFIED_DATE_PATTERN),
    },
    Const {
        name: "SERVING_CONSUMER_ORDER",
        value: ConstValue::List {
            ty: "ServingConsumer",
            items: SERVING_CONSUMER_ORDER,
        },
    },
    Const {
        name: "SERVING_REQUEST_SCHEMA_V1",
        value: ConstValue::Str(SERVING_REQUEST_SCHEMA_V1),
    },
    Const {
        name: "SERVING_REQUEST_ID_PATTERN",
        value: ConstValue::Regex(&SERVING_REQUEST_ID_PATTERN),
    },
    Const {
        name: "SERVING_STATUS_SCHEMA_V1",
        value: ConstValue::Str(SERVING_STATUS_SCHEMA_V1),
    },
    Const {
        name: "SERVING_VISIBILITY_SCHEMA_V1",
        value: ConstValue::Str(SERVING_VISIBILITY_SCHEMA_V1),
    },
    Const {
        name: "SERVING_STATUS_STALE_MS",
        value: ConstValue::Int(SERVING_STATUS_STALE_MS),
    },
];
