//! `apps/api/src/queue/eta.ts`: per-queue ETA from the queue's own record timestamps (legacy
//! global median), sharpened by the model/context cost table when the queue's records carry
//! `sweep_cell.model`. Reads `recordsDir` only; never throws (every unreadable cell is skipped).
use crate::jsrt;
use bd_domain::js::{math_round, trim};
use serde::Serialize;
use serde_json::{Map, Value};
use std::collections::HashMap;

/// A delta above this is the gap between nightly windows, not one cell's wall time.
const MAX_RETAINED_DELTA_SECONDS: f64 = 3600.0;

/// `round1`: `Math.round(value * 10) / 10`.
fn round1(v: f64) -> f64 {
    math_round(v * 10.0) / 10.0
}

/// `QueueEta`, in the TS object-literal key order; the model-table pair is appended only when
/// that basis is used (byte-identical legacy JSON otherwise).
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct QueueEta {
    pub remaining_cells: Option<f64>,
    pub median_cell_minutes: Option<f64>,
    pub estimated_minutes_remaining: Option<f64>,
    pub cells_per_hour: Option<f64>,
    pub basis: Option<&'static str>,
    pub sample_size: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bucket_sample_size: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scaling_caveat: Option<bool>,
}

/// Directory entries that are directories (no symlink follow, `Dirent.isDirectory()`), sorted
/// by UTF-16 code units; None when the dir cannot be read.
fn sorted_subdirs(dir: &str) -> Option<Vec<String>> {
    let mut names: Vec<String> = std::fs::read_dir(dir)
        .ok()?
        .filter_map(Result::ok)
        .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()))
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    names.sort_by(|a, b| jsrt::utf16_cmp(a, b));
    Some(names)
}

fn read_json_object(path: &str) -> Option<Map<String, Value>> {
    let text = std::fs::read(path).ok()?;
    match jsrt::json_parse(&String::from_utf8_lossy(&text)).ok()? {
        Value::Object(m) => Some(m),
        _ => None,
    }
}

/// `readQueueRecordTimestamps`.
pub fn read_queue_record_timestamps(records_dir: &str, queue_stem: &str) -> Vec<String> {
    let queue_dir = jsrt::path_join(records_dir, queue_stem);
    let Some(cells) = sorted_subdirs(&queue_dir) else {
        return Vec::new();
    };
    cells
        .iter()
        .filter_map(|c| {
            let cfg = read_json_object(&jsrt::path_join(
                &jsrt::path_join(&queue_dir, c),
                "config.json",
            ))?;
            cfg.get("timestamp")?.as_str().map(str::to_string)
        })
        .collect()
}

#[derive(Debug, Clone, PartialEq)]
pub struct CellTimeStats {
    pub median_cell_minutes: Option<f64>,
    pub sample_size: Option<f64>,
    pub contributing_timestamps: Vec<String>,
}

/// `computeCellTimeStats`.
pub fn compute_cell_time_stats(timestamps: &[String]) -> CellTimeStats {
    let mut parsed: Vec<(&String, f64)> = timestamps
        .iter()
        .filter_map(|raw| jsrt::date_parse(raw).map(|ms| (raw, ms)))
        .collect();
    parsed.sort_by(|a, b| a.1.total_cmp(&b.1));
    let mut retained: Vec<(f64, &String, &String)> = Vec::new();
    for w in parsed.windows(2) {
        let delta_seconds = (w[1].1 - w[0].1) / 1000.0;
        if delta_seconds <= MAX_RETAINED_DELTA_SECONDS {
            retained.push((delta_seconds / 60.0, w[0].0, w[1].0));
        }
    }
    if retained.is_empty() {
        return CellTimeStats {
            median_cell_minutes: None,
            sample_size: None,
            contributing_timestamps: Vec::new(),
        };
    }
    let deltas: Vec<f64> = retained.iter().map(|r| r.0).collect();
    let median = median_of(&deltas);
    let contributing = retained
        .iter()
        .flat_map(|r| [r.1.clone(), r.2.clone()])
        .collect();
    CellTimeStats {
        median_cell_minutes: median,
        sample_size: Some(retained.len() as f64),
        contributing_timestamps: contributing,
    }
}

/// `resolveBasis`.
pub fn resolve_basis(
    contributing: &[String],
    live_started_at: Option<&str>,
) -> Option<&'static str> {
    if contributing.is_empty() {
        return None;
    }
    let Some(live) = live_started_at else {
        return Some("prior-runs");
    };
    let Some(live_ms) = jsrt::date_parse(live) else {
        return Some("prior-runs");
    };
    let current = contributing
        .iter()
        .any(|raw| jsrt::date_parse(raw).is_some_and(|ms| ms >= live_ms));
    Some(if current { "current-run" } else { "prior-runs" })
}

/// `computeQueueEta`: the legacy global-median estimate.
pub fn compute_queue_eta(
    timestamps: &[String],
    remaining_cells: Option<f64>,
    live_started_at: Option<&str>,
) -> QueueEta {
    let stats = compute_cell_time_stats(timestamps);
    let basis = resolve_basis(&stats.contributing_timestamps, live_started_at);
    let m = stats.median_cell_minutes;
    QueueEta {
        remaining_cells,
        median_cell_minutes: m.map(round1),
        estimated_minutes_remaining: match (m, remaining_cells) {
            (Some(m), Some(r)) => Some(round1(r * m)),
            _ => None,
        },
        cells_per_hour: m.filter(|m| *m > 0.0).map(|m| round1(60.0 / m)),
        basis,
        sample_size: stats.sample_size,
        bucket_sample_size: None,
        scaling_caveat: None,
    }
}

/// `CostSample`.
#[derive(Debug, Clone, PartialEq)]
pub struct CostSample {
    pub model: String,
    pub context: f64,
    pub cost_seconds: f64,
    pub avg_prompt_tokens: Option<f64>,
}

fn finite(v: Option<&Value>) -> Option<f64> {
    jsrt::as_number(v)
}

fn record_model(cfg: &Map<String, Value>) -> Option<String> {
    cfg.get("sweep_cell")?
        .as_object()?
        .get("model")?
        .as_str()
        .map(str::to_string)
}

fn record_context(cfg: &Map<String, Value>) -> Option<f64> {
    let flags = cfg
        .get("sweep_cell")?
        .as_object()?
        .get("server_flags")?
        .as_object()?;
    finite(flags.get("-c"))
}

fn record_timestamp_diff_seconds(cfg: &Map<String, Value>) -> Option<f64> {
    let started = jsrt::date_parse(cfg.get("cell_started_at")?.as_str()?)?;
    let ended = jsrt::date_parse(cfg.get("cell_ended_at")?.as_str()?)?;
    (ended >= started).then(|| (ended - started) / 1000.0)
}

/// `readCellProbeMetrics`: (summed probe wall seconds, average prompt tokens).
fn read_cell_probe_metrics(cell_dir: &str) -> (Option<f64>, Option<f64>) {
    let Ok(bytes) = std::fs::read(jsrt::path_join(cell_dir, "samples.jsonl")) else {
        return (None, None);
    };
    let text = String::from_utf8_lossy(&bytes);
    let (mut wall, mut wall_n, mut tok, mut tok_n) = (0.0, 0u32, 0.0, 0u32);
    for line in text.split('\n') {
        let t = trim(line);
        if t.is_empty() {
            continue;
        }
        let Ok(Value::Object(row)) = jsrt::json_parse(t) else {
            continue;
        };
        if row.get("type").and_then(Value::as_str) != Some("longcontext_probe") {
            continue;
        }
        if let Some(w) = finite(row.get("wall_seconds")) {
            wall += w;
            wall_n += 1;
        }
        if let Some(p) = finite(row.get("prompt_tokens")).filter(|p| *p > 0.0) {
            tok += p;
            tok_n += 1;
        }
    }
    (
        (wall_n > 0).then_some(wall),
        (tok_n > 0).then(|| tok / f64::from(tok_n)),
    )
}

/// `readQueueCostSamples`.
pub fn read_queue_cost_samples(records_dir: &str, queue_stem: &str) -> Vec<CostSample> {
    let queue_dir = jsrt::path_join(records_dir, queue_stem);
    let Some(cells) = sorted_subdirs(&queue_dir) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for c in &cells {
        let cell_dir = jsrt::path_join(&queue_dir, c);
        let Some(cfg) = read_json_object(&jsrt::path_join(&cell_dir, "config.json")) else {
            continue;
        };
        let (Some(model), Some(context)) = (record_model(&cfg), record_context(&cfg)) else {
            continue;
        };
        let (summed, avg) = read_cell_probe_metrics(&cell_dir);
        let Some(cost_seconds) = finite(cfg.get("cell_wall_seconds"))
            .or_else(|| record_timestamp_diff_seconds(&cfg))
            .or(summed)
        else {
            continue;
        };
        out.push(CostSample {
            model,
            context,
            cost_seconds,
            avg_prompt_tokens: avg,
        });
    }
    out
}

/// `medianOf`: even counts average the middle pair.
pub fn median_of(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let mid = sorted.len() / 2;
    Some(if sorted.len() % 2 == 0 {
        (sorted[mid - 1] + sorted[mid]) / 2.0
    } else {
        sorted[mid]
    })
}

fn bucket_key(model: &str, context: f64) -> String {
    format!("{model}::{}", jsrt::num_str(context))
}

/// `CellDescriptor`.
#[derive(Debug, Clone, PartialEq)]
pub struct CellDescriptor {
    pub model: Option<String>,
    pub c: Option<f64>,
    pub state: Option<String>,
}

/// `applyModelTableEta`: tier 1 exact (model, context) median, tier 2 same-model
/// seconds-per-prompt-token scaled to `c`, tier 3 the legacy median; `legacy` unchanged when
/// no remaining cell resolves through the table.
pub fn apply_model_table_eta(
    legacy: QueueEta,
    samples: &[CostSample],
    descriptors: &[CellDescriptor],
) -> QueueEta {
    let remaining: Vec<&CellDescriptor> = descriptors
        .iter()
        .filter(|d| matches!(d.state.as_deref(), Some("pending" | "running")))
        .collect();
    if remaining.is_empty() || samples.is_empty() {
        return legacy;
    }
    let mut exact: HashMap<String, Vec<&CostSample>> = HashMap::new();
    let mut by_model: HashMap<&str, Vec<&CostSample>> = HashMap::new();
    for s in samples {
        exact
            .entry(bucket_key(&s.model, s.context))
            .or_default()
            .push(s);
        by_model.entry(&s.model).or_default().push(s);
    }
    let resolve_exact = |model: &str, c: f64| -> Option<(f64, usize)> {
        let bucket = exact.get(&bucket_key(model, c)).filter(|b| !b.is_empty())?;
        let costs: Vec<f64> = bucket.iter().map(|s| s.cost_seconds).collect();
        Some((median_of(&costs)? / 60.0, bucket.len()))
    };
    let resolve_scaled = |model: &str, c: f64| -> Option<(f64, usize)> {
        let with_tokens: Vec<&&CostSample> = by_model
            .get(model)?
            .iter()
            .filter(|s| s.avg_prompt_tokens.is_some_and(|t| t > 0.0))
            .collect();
        if with_tokens.is_empty() {
            return None;
        }
        let rates: Vec<f64> = with_tokens
            .iter()
            .map(|s| s.cost_seconds / s.avg_prompt_tokens.unwrap_or(1.0))
            .collect();
        let rate = median_of(&rates).filter(|r| r.is_finite())?;
        Some((rate * c / 60.0, with_tokens.len()))
    };

    let legacy_minutes = legacy.median_cell_minutes;
    let (mut used_table, mut used_non_exact) = (false, false);
    let mut bucket_sample_size = 0usize;
    let mut resolved: Vec<f64> = Vec::new();
    for cell in remaining {
        let key = cell.model.as_deref().zip(cell.c);
        if let Some((minutes, n)) = key.and_then(|(m, c)| resolve_exact(m, c)) {
            used_table = true;
            bucket_sample_size += n;
            resolved.push(minutes);
            continue;
        }
        if let Some((minutes, n)) = key.and_then(|(m, c)| resolve_scaled(m, c)) {
            used_table = true;
            used_non_exact = true;
            bucket_sample_size += n;
            resolved.push(minutes);
            continue;
        }
        used_non_exact = true;
        if let Some(m) = legacy_minutes {
            resolved.push(m);
        }
    }
    if !used_table {
        return legacy;
    }
    let median = median_of(&resolved);
    let total: f64 = resolved.iter().fold(0.0, |a, m| a + m);
    QueueEta {
        basis: Some("model-table"),
        median_cell_minutes: median.map(round1),
        estimated_minutes_remaining: Some(round1(total)),
        cells_per_hour: median.filter(|m| *m > 0.0).map(|m| round1(60.0 / m)),
        bucket_sample_size: Some(bucket_sample_size as f64),
        scaling_caveat: Some(used_non_exact),
        ..legacy
    }
}

/// `computeEtaForQueue`.
pub fn compute_eta_for_queue(
    records_dir: Option<&str>,
    queue_stem: &str,
    remaining_cells: Option<f64>,
    live_started_at: Option<&str>,
    descriptors: &[CellDescriptor],
) -> QueueEta {
    let timestamps = records_dir
        .map(|d| read_queue_record_timestamps(d, queue_stem))
        .unwrap_or_default();
    let legacy = compute_queue_eta(&timestamps, remaining_cells, live_started_at);
    let Some(dir) = records_dir else {
        return legacy;
    };
    if descriptors.is_empty() {
        return legacy;
    }
    let samples = read_queue_cost_samples(dir, queue_stem);
    apply_model_table_eta(legacy, &samples, descriptors)
}

#[cfg(test)]
#[path = "eta_tests.rs"]
mod tests;
