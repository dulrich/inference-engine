//! `apps/api/src/ingest/record.ts`: read one `records/<experiment-id>/` dir and classify it
//! `v2` (normalized), `v1` or `invalid`; the v1 timing-only side door; the two-level scan.
//! JSON-parse and I/O error text in `invalid` reasons is Rust's (user decision 2026-10-15),
//! except a missing file, which keeps Bun's `ENOENT: no such file or directory, open '<path>'`.
use crate::jsrt::{self, as_number, path_join};
use bd_domain::contract::WILSON_SUMMARY;
use bd_domain::validate;
use serde_json::{Map, Value};
use std::path::Path;

#[derive(Debug, Clone, PartialEq)]
pub struct NormalizedTrial {
    pub line_index: usize,
    pub kind: String,
    pub trial: Option<f64>,
    pub sampling_arm: Option<String>,
    pub temperature: Option<f64>,
    pub top_p: Option<f64>,
    pub top_k: Option<f64>,
    pub sampler_seed: Option<f64>,
    pub haystack_seed: Option<f64>,
    pub corpus_manifest_id: Option<String>,
    pub haystack_sha256: Option<String>,
    pub classifier_verdict: Option<String>,
    pub classifier_shape: Option<String>,
    pub prompt_tokens: Option<f64>,
    pub completion_tokens: Option<f64>,
    pub finish_reason: Option<String>,
    pub position_fraction: Option<f64>,
    pub task: Option<String>,
    pub cell_id: Option<String>,
    pub correct: Option<bool>,
    pub raw: Value,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NormalizedMetric {
    pub line_index: Option<usize>,
    pub metric_type: String,
    pub round_type: Option<String>,
    pub round_index: Option<f64>,
    pub bytes: Option<f64>,
    pub first_generation_token_ms: Option<f64>,
    pub observed_decode_tok_s: Option<f64>,
    pub prompt_tokens: Option<f64>,
    pub cached_tokens: Option<f64>,
    pub cache_hit_ratio: Option<f64>,
    pub cache_assertion_passed: Option<bool>,
    pub raw: Value,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NormalizedRecord {
    pub id: String,
    pub description: Option<String>,
    pub timestamp: Option<String>,
    pub hf_repo: Option<String>,
    pub hf_revision: Option<String>,
    pub filename: Option<String>,
    pub file_size: Option<f64>,
    pub sha256: Option<String>,
    pub llama_cpp_build: Option<String>,
    pub sweep_cell_id: Option<String>,
    pub sweep_alias: Option<String>,
    /// `cell.server_flags ?? null`: any JSON value, `None` for absent/null.
    pub sweep_server_flags: Option<Value>,
    pub sweep_command: Option<String>,
    pub sampling_arm: Option<String>,
    pub battery: Option<Vec<String>>,
    pub model_host_path: Option<String>,
    pub vram_profile: Option<String>,
    /// The `WilsonSummarySchema` parse output (stripped, declared key order); `None` = null.
    pub wilson_summary: Option<Value>,
    pub raw_config: Value,
    pub raw_prompts: Vec<Value>,
    pub raw_completions: Vec<Value>,
    pub trials: Vec<NormalizedTrial>,
    pub metrics: Vec<NormalizedMetric>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum ReadResult {
    V2(Box<NormalizedRecord>),
    V1,
    Invalid(String),
}

#[derive(Debug, Clone, PartialEq)]
pub enum LegacyResult {
    V1Timing(Box<NormalizedRecord>),
    V1,
    Invalid(String),
}

fn as_string(v: Option<&Value>) -> Option<String> {
    v.and_then(Value::as_str).map(str::to_string)
}
fn as_bool(v: Option<&Value>) -> Option<bool> {
    v.and_then(Value::as_bool)
}
fn as_string_array(v: Option<&Value>) -> Option<Vec<String>> {
    v?.as_array()?
        .iter()
        .map(|x| x.as_str().map(str::to_string))
        .collect()
}
static EMPTY: std::sync::LazyLock<Map<String, Value>> = std::sync::LazyLock::new(Map::new);
fn obj(v: Option<&Value>) -> &Map<String, Value> {
    v.and_then(Value::as_object).unwrap_or(&EMPTY)
}

/// `Bun.file(path).text()`.
fn read_text(path: &str) -> Result<String, String> {
    match std::fs::read(path) {
        Ok(bytes) => Ok(String::from_utf8_lossy(&bytes).into_owned()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Err(format!("ENOENT: no such file or directory, open '{path}'"))
        }
        Err(e) => Err(format!("{e}, open '{path}'")),
    }
}

fn read_json(path: &str) -> Result<Value, String> {
    let text = read_text(path)?;
    jsrt::json_parse(&text).map_err(|e| e.to_string())
}

/// `readJsonl`: one entry per non-blank line; the first malformed line fails the file with
/// `<path>:<n>: <parse error>` (n counts non-blank lines).
pub fn read_jsonl(path: &str) -> Result<Vec<Value>, String> {
    let text = read_text(path)?;
    text.split('\n')
        .filter(|l| !bd_domain::js::trim(l).is_empty())
        .enumerate()
        .map(|(i, line)| jsrt::json_parse(line).map_err(|e| format!("{path}:{}: {e}", i + 1)))
        .collect()
}

struct Sweep {
    cell_id: Option<String>,
    alias: Option<String>,
    server_flags: Option<Value>,
    command: Option<String>,
    sampling_arm: Option<String>,
    battery: Option<Vec<String>>,
    model_host_path: Option<String>,
}

fn normalize_sweep_cell(config: &Map<String, Value>) -> Sweep {
    let Some(cell) = config.get("sweep_cell").and_then(Value::as_object) else {
        return Sweep {
            cell_id: None,
            alias: None,
            server_flags: None,
            command: None,
            sampling_arm: None,
            battery: None,
            model_host_path: None,
        };
    };
    Sweep {
        cell_id: as_string(cell.get("cell_id")),
        alias: as_string(cell.get("alias")),
        server_flags: cell.get("server_flags").filter(|v| !v.is_null()).cloned(),
        command: as_string(cell.get("command")),
        sampling_arm: as_string(cell.get("sampling_arm")),
        battery: as_string_array(cell.get("battery")),
        model_host_path: as_string(cell.get("model_host_path")),
    }
}

pub fn normalize_trial(row: &Value, line_index: usize) -> NormalizedTrial {
    let r = obj(Some(row));
    NormalizedTrial {
        line_index,
        kind: as_string(r.get("type")).unwrap_or_else(|| "unknown".into()),
        trial: as_number(r.get("trial")).or_else(|| as_number(r.get("trial_index"))),
        sampling_arm: as_string(r.get("sampling_arm")),
        temperature: as_number(r.get("temperature")),
        top_p: as_number(r.get("top_p")),
        top_k: as_number(r.get("top_k")),
        sampler_seed: as_number(r.get("sampler_seed")),
        haystack_seed: as_number(r.get("haystack_seed")),
        corpus_manifest_id: as_string(r.get("corpus_manifest_id")),
        haystack_sha256: as_string(r.get("haystack_sha256")),
        classifier_verdict: as_string(r.get("classifier_verdict")),
        classifier_shape: as_string(r.get("classifier_shape")),
        prompt_tokens: as_number(r.get("prompt_tokens")),
        completion_tokens: as_number(r.get("completion_tokens")),
        finish_reason: as_string(r.get("finish_reason")),
        position_fraction: as_number(r.get("position_fraction")),
        task: as_string(r.get("task")),
        cell_id: as_string(r.get("cell_id")),
        correct: as_bool(r.get("correct")),
        raw: row.clone(),
    }
}

pub fn normalize_timing_metric(row: &Value, line_index: usize) -> NormalizedMetric {
    let r = obj(Some(row));
    NormalizedMetric {
        line_index: Some(line_index),
        metric_type: "timing_round".into(),
        round_type: as_string(r.get("run_state")),
        round_index: as_number(r.get("round_index")),
        bytes: None,
        first_generation_token_ms: as_number(r.get("first_generation_token_ms")),
        observed_decode_tok_s: as_number(r.get("observed_decode_tok_s")),
        prompt_tokens: as_number(r.get("prompt_tokens")),
        cached_tokens: as_number(r.get("cached_tokens")),
        cache_hit_ratio: as_number(r.get("cache_hit_ratio")),
        cache_assertion_passed: as_bool(r.get("cache_assertion_passed")),
        raw: row.clone(),
    }
}

pub fn vram_metrics(config: &Map<String, Value>) -> Vec<NormalizedMetric> {
    let Some(vram) = config.get("vram").filter(|v| v.is_object()) else {
        return Vec::new();
    };
    [
        ("baseline_bytes", "vram_baseline"),
        ("loaded_bytes", "vram_loaded"),
        ("after_teardown_bytes", "vram_after_teardown"),
    ]
    .iter()
    .filter_map(|(key, metric_type)| {
        let bytes = as_number(vram.get(*key))?;
        Some(NormalizedMetric {
            line_index: None,
            metric_type: (*metric_type).into(),
            round_type: None,
            round_index: None,
            bytes: Some(bytes),
            first_generation_token_ms: None,
            observed_decode_tok_s: None,
            prompt_tokens: None,
            cached_tokens: None,
            cache_hit_ratio: None,
            cache_assertion_passed: None,
            raw: vram.clone(),
        })
    })
    .collect()
}

fn is_timing_round(row: &Value) -> bool {
    row.get("type").and_then(Value::as_str) == Some("timing_round")
}

/// `config.json` read + object check, shared by both readers.
fn read_config(dir: &str) -> Result<Map<String, Value>, String> {
    let config =
        read_json(&path_join(dir, "config.json")).map_err(|e| format!("config.json: {e}"))?;
    match config {
        Value::Object(m) => Ok(m),
        _ => Err("config.json is not a JSON object".into()),
    }
}

fn is_schema_v2(config: &Map<String, Value>) -> bool {
    config.get("schema_version").and_then(Value::as_f64) == Some(2.0)
}

/// zod's `error.message`: the issues array, `JSON.stringify(issues, null, 2)`.
fn zod_message(issues: &[validate::Issue]) -> String {
    serde_json::to_string_pretty(&validate::issues_json(issues)).unwrap_or_default()
}

pub fn read_record_dir(dir: &str) -> ReadResult {
    let id = jsrt::basename(dir).to_string();
    let config = match read_config(dir) {
        Ok(c) => c,
        Err(reason) => return ReadResult::Invalid(reason),
    };
    if !is_schema_v2(&config) {
        return ReadResult::V1;
    }
    let wilson_input = config.get("wilson_summary").cloned().unwrap_or(Value::Null);
    let wilson_summary = match validate::parse(&WILSON_SUMMARY.node, &wilson_input) {
        Ok(Value::Null) => None,
        Ok(v) => Some(v),
        Err(issues) => {
            return ReadResult::Invalid(format!(
                "config.json: wilson_summary: {}",
                zod_message(&issues)
            ))
        }
    };
    let read = |name: &str| read_jsonl(&path_join(dir, name));
    let (prompts, completions, samples) = match (|| {
        Ok::<_, String>((
            read("prompts.jsonl")?,
            read("completions.jsonl")?,
            read("samples.jsonl")?,
        ))
    })() {
        Ok(t) => t,
        Err(reason) => return ReadResult::Invalid(reason),
    };
    let lineage = obj(config.get("lineage"));
    let sweep = normalize_sweep_cell(&config);
    let mut trials = Vec::new();
    let mut metrics = vram_metrics(&config);
    for (i, row) in samples.iter().enumerate() {
        if is_timing_round(row) {
            metrics.push(normalize_timing_metric(row, i));
        } else {
            trials.push(normalize_trial(row, i));
        }
    }
    ReadResult::V2(Box::new(NormalizedRecord {
        id,
        description: as_string(config.get("description")),
        timestamp: as_string(config.get("timestamp")),
        hf_repo: as_string(lineage.get("hf_repo")),
        hf_revision: as_string(lineage.get("hf_revision")),
        filename: as_string(lineage.get("filename")),
        file_size: as_number(lineage.get("file_size")),
        sha256: as_string(lineage.get("sha256")),
        llama_cpp_build: as_string(lineage.get("llama_cpp_build")),
        sweep_cell_id: sweep.cell_id,
        sweep_alias: sweep.alias,
        sweep_server_flags: sweep.server_flags,
        sweep_command: sweep.command,
        sampling_arm: sweep.sampling_arm,
        battery: sweep.battery,
        model_host_path: sweep.model_host_path,
        vram_profile: as_string(config.get("vram_profile")),
        wilson_summary,
        raw_config: Value::Object(config),
        raw_prompts: prompts,
        raw_completions: completions,
        trials,
        metrics,
    }))
}

fn legacy_alias(config: &Map<String, Value>) -> Option<String> {
    as_string(config.get("alias")).or_else(|| {
        config
            .get("sweep_cell")
            .and_then(Value::as_object)
            .and_then(|c| as_string(c.get("alias")))
    })
}

/// `readLegacyTimingRecord`: the allowlisted v1 timing-only side door.
pub fn read_legacy_timing_record(dir: &str) -> LegacyResult {
    let id = jsrt::basename(dir).to_string();
    let config = match read_config(dir) {
        Ok(c) => c,
        Err(reason) => return LegacyResult::Invalid(reason),
    };
    if is_schema_v2(&config) {
        return LegacyResult::V1;
    }
    let samples_path = path_join(dir, "samples.jsonl");
    if !Path::new(&samples_path).exists() {
        return LegacyResult::V1;
    }
    let samples = match read_jsonl(&samples_path) {
        Ok(s) => s,
        Err(reason) => return LegacyResult::Invalid(reason),
    };
    let metrics: Vec<NormalizedMetric> = samples
        .iter()
        .enumerate()
        .filter(|(_, row)| is_timing_round(row))
        .map(|(i, row)| normalize_timing_metric(row, i))
        .collect();
    if metrics.is_empty() {
        return LegacyResult::V1;
    }
    let lineage = obj(config.get("lineage"));
    LegacyResult::V1Timing(Box::new(NormalizedRecord {
        id,
        description: as_string(config.get("description")),
        timestamp: as_string(config.get("timestamp")),
        hf_repo: as_string(lineage.get("hf_repo")),
        hf_revision: as_string(lineage.get("hf_revision")),
        filename: as_string(lineage.get("filename")),
        file_size: as_number(lineage.get("file_size")),
        sha256: as_string(lineage.get("sha256")),
        llama_cpp_build: as_string(lineage.get("llama_cpp_build")),
        sweep_cell_id: None,
        sweep_alias: legacy_alias(&config),
        sweep_server_flags: None,
        sweep_command: None,
        sampling_arm: None,
        battery: None,
        model_host_path: None,
        vram_profile: None,
        wilson_summary: None,
        raw_config: Value::Object(config),
        raw_prompts: Vec::new(),
        raw_completions: Vec::new(),
        trials: Vec::new(),
        metrics,
    }))
}

fn has_config_json(dir: &str) -> bool {
    Path::new(&path_join(dir, "config.json")).exists()
}

/// Immediate subdirectory names of `dir` (symlinks are not directories, `Dirent.isDirectory`).
fn dir_names(dir: &str) -> std::io::Result<Vec<String>> {
    let mut names = Vec::new();
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            names.push(entry.file_name().to_string_lossy().into_owned());
        }
    }
    Ok(names)
}

fn sorted_children(dir: &str, names: Vec<String>) -> Vec<String> {
    let mut out: Vec<String> = names.iter().map(|n| path_join(dir, n)).collect();
    out.sort_by(|a, b| jsrt::utf16_cmp(a, b));
    out
}

/// `listRecordDirs`: the two-level-aware scan. An unreadable `records_dir` is an error.
pub fn list_record_dirs(records_dir: &str) -> Result<Vec<String>, String> {
    let names = dir_names(records_dir).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            format!("ENOENT: no such file or directory, scandir '{records_dir}'")
        } else {
            format!("{e}, scandir '{records_dir}'")
        }
    })?;
    let mut result = Vec::new();
    for dir in sorted_children(records_dir, names) {
        if has_config_json(&dir) {
            result.push(dir);
            continue;
        }
        let subs = dir_names(&dir)
            .map(|n| sorted_children(&dir, n))
            .unwrap_or_default();
        if subs.iter().any(|s| has_config_json(s)) {
            result.extend(subs);
        } else {
            result.push(dir);
        }
    }
    Ok(result)
}

#[cfg(test)]
#[path = "record_tests.rs"]
pub(crate) mod tests;
