//! `apps/api/src/ingest/{run,cli}.ts`: `inference-dashboard ingest [--dir <path>|--dir=<path>]
//! [--rebuild]` (plan Decision 6).
use crate::db::Db;
use crate::record::{
    list_record_dirs, read_legacy_timing_record, read_record_dir, LegacyResult, ReadResult,
};
use serde::Serialize;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliOptions {
    pub dir: String,
    pub rebuild: bool,
}

/// `parseArgs`: the error text is the TS `Error` message.
pub fn parse_args(argv: &[String], default_dir: &str) -> Result<CliOptions, String> {
    let mut options = CliOptions {
        dir: default_dir.to_string(),
        rebuild: false,
    };
    let mut i = 0;
    while i < argv.len() {
        let arg = argv[i].as_str();
        if arg == "--rebuild" {
            options.rebuild = true;
        } else if arg == "--dir" {
            let value = argv.get(i + 1).ok_or("--dir requires a value")?;
            options.dir = value.clone();
            i += 1;
        } else if let Some(v) = arg.strip_prefix("--dir=") {
            options.dir = v.to_string();
        } else {
            return Err(format!("unknown argument: {arg}"));
        }
        i += 1;
    }
    Ok(options)
}

/// `IngestSummary`, in the TS literal's key order.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct IngestSummary {
    pub scanned: usize,
    pub ingested: usize,
    pub skipped_v1: usize,
    pub skipped_invalid: usize,
    pub legacy_timing_ingested: usize,
    pub rebuilt: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    V2,
    V1,
    V1Timing,
    Invalid(String),
}

/// `runIngest`. `--rebuild` clears before the scan (so an unreadable dir after `--rebuild`
/// still leaves the tables empty, as in Bun).
pub fn run_ingest(
    db: &Db,
    records_dir: &str,
    rebuild: bool,
    mut on_record: impl FnMut(&str, &Event),
) -> Result<IngestSummary, String> {
    if rebuild {
        db.clear_all_experiments()?;
    }
    let dirs = list_record_dirs(records_dir)?;
    let mut s = IngestSummary {
        scanned: dirs.len(),
        ingested: 0,
        skipped_v1: 0,
        skipped_invalid: 0,
        legacy_timing_ingested: 0,
        rebuilt: rebuild,
    };
    for dir in &dirs {
        let event = match read_record_dir(dir) {
            ReadResult::V2(record) => {
                db.replace_experiment(&record)?;
                s.ingested += 1;
                Event::V2
            }
            ReadResult::V1 => match read_legacy_timing_record(dir) {
                LegacyResult::V1Timing(record) => {
                    db.replace_experiment(&record)?;
                    db.mark_legacy_timing_provenance(&record.id)?;
                    s.legacy_timing_ingested += 1;
                    Event::V1Timing
                }
                LegacyResult::V1 => {
                    s.skipped_v1 += 1;
                    Event::V1
                }
                LegacyResult::Invalid(reason) => {
                    s.skipped_invalid += 1;
                    Event::Invalid(reason)
                }
            },
            ReadResult::Invalid(reason) => {
                s.skipped_invalid += 1;
                Event::Invalid(reason)
            }
        };
        on_record(dir, &event);
    }
    Ok(s)
}

/// The CLI body: `[ingest] invalid: <dir> (<reason>)` lines on stderr, the summary JSON line on
/// stdout. Exit 1 on an argument error or a failed scan (Bun's uncaught-error exit code); the
/// error text is Rust-style (user decision 2026-10-15).
pub fn cli(db: &Db, argv: &[String], default_dir: &str) -> u8 {
    let options = match parse_args(argv, default_dir) {
        Ok(o) => o,
        Err(e) => {
            eprintln!("inference-dashboard ingest: error: {e}");
            return 1;
        }
    };
    let result = run_ingest(db, &options.dir, options.rebuild, |dir, event| {
        if let Event::Invalid(reason) = event {
            eprintln!("[ingest] invalid: {dir} ({reason})");
        }
    });
    match result {
        Ok(summary) => {
            let v = serde_json::to_value(&summary).unwrap_or_default();
            println!("{}", crate::jsrt::stringify(&v));
            0
        }
        Err(e) => {
            eprintln!("inference-dashboard ingest: error: {e}");
            1
        }
    }
}

#[cfg(test)]
#[path = "ingest_tests.rs"]
mod tests;
