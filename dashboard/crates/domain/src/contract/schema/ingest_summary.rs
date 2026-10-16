//! `ingestSummary.ts`.
use super::*;

pub const INGEST_SUMMARY: Export = Export::out(
    "IngestSummary",
    obj(&[
        f("scanned", &NONNEG_INT),
        f("ingested", &NONNEG_INT),
        f("skippedV1", &NONNEG_INT),
        f("skippedInvalid", &NONNEG_INT),
        f("legacyTimingIngested", &NONNEG_INT),
        f("rebuilt", &BOOL),
    ]),
);
