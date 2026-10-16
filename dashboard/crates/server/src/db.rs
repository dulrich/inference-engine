//! `apps/api/src/db/experiments.ts`: the repository over the four tables. Every statement
//! names its columns; numeric columns read as `f64` (JS numbers — an INTEGER-affinity column
//! may hold a REAL the record carried). Replies are typed `Serialize` DTOs in the key order the
//! Bun handlers build them.
use crate::jsrt::{self, json_parse, stringify};
use crate::record::NormalizedRecord;
use crate::schema::open_database;
use crate::serving::{resolve_served_model, ResolvableTimingRow, ResolvedServedModel, ServedModel};
use bd_domain::contract::WILSON_SUMMARY;
use bd_domain::validate;
use rusqlite::types::Value as Sql;
use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::Serialize;
use serde_json::{Map, Value};
use std::sync::{Mutex, MutexGuard};

pub struct Db {
    conn: Mutex<Connection>,
}

pub type DbResult<T> = Result<T, String>;

fn e(err: rusqlite::Error) -> String {
    err.to_string()
}

fn parse(text: &str) -> DbResult<Value> {
    json_parse(text).map_err(|err| format!("JSON.parse: {err}"))
}

#[derive(Serialize, Debug, Clone, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Lineage {
    pub hf_repo: Option<String>,
    pub hf_revision: Option<String>,
    pub filename: Option<String>,
    pub file_size: Option<f64>,
    pub sha256: Option<String>,
    pub llama_cpp_build: Option<String>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct SweepCell {
    pub cell_id: String,
    pub alias: String,
    pub server_flags: Value,
    pub command: Option<String>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ExperimentSummary {
    pub id: String,
    pub schema_version: u8,
    pub description: Option<String>,
    pub timestamp: Option<String>,
    pub lineage: Lineage,
    pub sweep_cell: Option<SweepCell>,
    pub has_wilson_summary: bool,
    pub sampling_arm: Option<String>,
    pub battery: Option<Value>,
    pub vram_profile: Option<String>,
    pub trial_count: f64,
    pub metric_count: f64,
}

/// `GET /api/experiments/:id`: `{...detail, trials, metrics}`.
#[derive(Serialize, Debug)]
#[serde(rename_all = "camelCase")]
pub struct ExperimentFullDetail {
    #[serde(flatten)]
    pub summary: ExperimentSummary,
    pub wilson_summary: Option<Value>,
    pub response_caps: Option<Value>,
    pub trials: Vec<TrialRow>,
    pub metrics: Vec<MetricRow>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct TrialRow {
    pub id: i64,
    pub experiment_id: String,
    pub line_index: f64,
    #[serde(rename = "type")]
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

#[derive(Serialize, Debug, Clone, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct MetricRow {
    pub id: i64,
    pub experiment_id: String,
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

#[derive(Serialize, Debug, Clone, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct LadderRow {
    pub experiment_id: String,
    pub alias: Option<String>,
    pub cell_id: Option<String>,
    pub filename: Option<String>,
    pub context_length: Option<f64>,
    pub kv_cache_type_k: Option<String>,
    pub kv_cache_type_v: Option<String>,
    pub flash_attention: Option<String>,
    pub median_decode_tok_s: Option<f64>,
    pub median_first_generation_token_ms: Option<f64>,
    pub correctness_rate: Option<f64>,
    pub trial_count: f64,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ServingTimingRow {
    pub model_key: String,
    pub aliases: Vec<String>,
    pub p50_prefill_tok_s: Option<f64>,
    pub p90_prefill_tok_s: Option<f64>,
    pub p50_decode_tok_s: Option<f64>,
    pub p90_decode_tok_s: Option<f64>,
    pub sampling_arm: Option<String>,
    pub sample_count: f64,
    pub mtp: bool,
}

/// `CaptureRecord`: the verbatim `config`/`prompts`/`completions`/`samples` are raw JSON by
/// contract (plan Decision 5's one exception).
#[derive(Serialize, Debug)]
#[serde(rename_all = "camelCase")]
pub struct CaptureRecord {
    pub experiment_id: String,
    pub config: Value,
    pub prompts: Value,
    pub completions: Value,
    pub samples: Vec<Value>,
    pub serving: Option<ResolvedServedModel>,
}

/// A `longcontext_probe` trial's curve inputs (`listLongcontextTrials`).
pub struct CurveTrial {
    pub task: Option<String>,
    pub position_fraction: Option<f64>,
    pub classifier_verdict: Option<String>,
}

const SUMMARY_COLUMNS: &str = "
  e.id, e.schema_version, e.description, e.timestamp, e.hf_repo, e.hf_revision, e.filename,
  e.file_size, e.sha256, e.llama_cpp_build, e.sweep_cell_id, e.sweep_alias,
  e.sweep_server_flags, e.sweep_command, e.wilson_summary_json, e.sampling_arm,
  e.battery_json, e.vram_profile, e.raw_config_json, e.raw_prompts_json,
  e.raw_completions_json, e.ingested_at,
  (SELECT COUNT(*) FROM trials t WHERE t.experiment_id = e.id) AS trial_count,
  (SELECT COUNT(*) FROM metrics m WHERE m.experiment_id = e.id) AS metric_count
";

const TRIAL_COLUMNS: &str = "id, experiment_id, line_index, type, trial, sampling_arm, temperature, top_p, top_k, sampler_seed, haystack_seed, corpus_manifest_id, haystack_sha256, classifier_verdict, classifier_shape, prompt_tokens, completion_tokens, finish_reason, position_fraction, task, cell_id, correct, raw_json";

const METRIC_COLUMNS: &str = "id, experiment_id, line_index, metric_type, round_type, round_index, bytes, first_generation_token_ms, observed_decode_tok_s, prompt_tokens, cached_tokens, cache_hit_ratio, cache_assertion_passed, raw_json";

/// The `SUMMARY_COLUMNS` row, read by name.
struct ExperimentRow {
    id: String,
    description: Option<String>,
    timestamp: Option<String>,
    hf_repo: Option<String>,
    hf_revision: Option<String>,
    filename: Option<String>,
    file_size: Option<f64>,
    sha256: Option<String>,
    llama_cpp_build: Option<String>,
    sweep_cell_id: Option<String>,
    sweep_alias: Option<String>,
    sweep_server_flags: Option<String>,
    sweep_command: Option<String>,
    wilson_summary_json: Option<String>,
    sampling_arm: Option<String>,
    battery_json: Option<String>,
    vram_profile: Option<String>,
    raw_config_json: String,
    trial_count: f64,
    metric_count: f64,
}

fn experiment_row(r: &Row) -> rusqlite::Result<ExperimentRow> {
    Ok(ExperimentRow {
        id: r.get("id")?,
        description: r.get("description")?,
        timestamp: r.get("timestamp")?,
        hf_repo: r.get("hf_repo")?,
        hf_revision: r.get("hf_revision")?,
        filename: r.get("filename")?,
        file_size: r.get("file_size")?,
        sha256: r.get("sha256")?,
        llama_cpp_build: r.get("llama_cpp_build")?,
        sweep_cell_id: r.get("sweep_cell_id")?,
        sweep_alias: r.get("sweep_alias")?,
        sweep_server_flags: r.get("sweep_server_flags")?,
        sweep_command: r.get("sweep_command")?,
        wilson_summary_json: r.get("wilson_summary_json")?,
        sampling_arm: r.get("sampling_arm")?,
        battery_json: r.get("battery_json")?,
        vram_profile: r.get("vram_profile")?,
        raw_config_json: r.get("raw_config_json")?,
        trial_count: r.get("trial_count")?,
        metric_count: r.get("metric_count")?,
    })
}

/// A JS-truthy stored string (`row.x ? JSON.parse(row.x) : …`).
fn truthy(s: &Option<String>) -> Option<&str> {
    s.as_deref().filter(|s| !s.is_empty())
}

fn row_to_summary(row: &ExperimentRow) -> DbResult<ExperimentSummary> {
    let sweep_cell = match &row.sweep_cell_id {
        None => None,
        Some(cell_id) => Some(SweepCell {
            cell_id: cell_id.clone(),
            alias: row.sweep_alias.clone().unwrap_or_default(),
            server_flags: match truthy(&row.sweep_server_flags) {
                Some(s) => parse(s)?,
                None => Value::Object(Map::new()),
            },
            command: row.sweep_command.clone(),
        }),
    };
    Ok(ExperimentSummary {
        id: row.id.clone(),
        schema_version: 2,
        description: row.description.clone(),
        timestamp: row.timestamp.clone(),
        lineage: Lineage {
            hf_repo: row.hf_repo.clone(),
            hf_revision: row.hf_revision.clone(),
            filename: row.filename.clone(),
            file_size: row.file_size,
            sha256: row.sha256.clone(),
            llama_cpp_build: row.llama_cpp_build.clone(),
        },
        sweep_cell,
        has_wilson_summary: row.wilson_summary_json.is_some(),
        sampling_arm: row.sampling_arm.clone(),
        battery: truthy(&row.battery_json).map(parse).transpose()?,
        vram_profile: row.vram_profile.clone(),
        trial_count: row.trial_count,
        metric_count: row.metric_count,
    })
}

fn row_to_trial(r: &Row) -> rusqlite::Result<(TrialRow, String)> {
    let correct: Option<f64> = r.get("correct")?;
    Ok((
        TrialRow {
            id: r.get("id")?,
            experiment_id: r.get("experiment_id")?,
            line_index: r.get("line_index")?,
            kind: r.get("type")?,
            trial: r.get("trial")?,
            sampling_arm: r.get("sampling_arm")?,
            temperature: r.get("temperature")?,
            top_p: r.get("top_p")?,
            top_k: r.get("top_k")?,
            sampler_seed: r.get("sampler_seed")?,
            haystack_seed: r.get("haystack_seed")?,
            corpus_manifest_id: r.get("corpus_manifest_id")?,
            haystack_sha256: r.get("haystack_sha256")?,
            classifier_verdict: r.get("classifier_verdict")?,
            classifier_shape: r.get("classifier_shape")?,
            prompt_tokens: r.get("prompt_tokens")?,
            completion_tokens: r.get("completion_tokens")?,
            finish_reason: r.get("finish_reason")?,
            position_fraction: r.get("position_fraction")?,
            task: r.get("task")?,
            cell_id: r.get("cell_id")?,
            correct: correct.map(|c| c == 1.0),
            raw: Value::Null,
        },
        r.get("raw_json")?,
    ))
}

fn row_to_metric(r: &Row) -> rusqlite::Result<(MetricRow, String)> {
    let passed: Option<f64> = r.get("cache_assertion_passed")?;
    Ok((
        MetricRow {
            id: r.get("id")?,
            experiment_id: r.get("experiment_id")?,
            metric_type: r.get("metric_type")?,
            round_type: r.get("round_type")?,
            round_index: r.get("round_index")?,
            bytes: r.get("bytes")?,
            first_generation_token_ms: r.get("first_generation_token_ms")?,
            observed_decode_tok_s: r.get("observed_decode_tok_s")?,
            prompt_tokens: r.get("prompt_tokens")?,
            cached_tokens: r.get("cached_tokens")?,
            cache_hit_ratio: r.get("cache_hit_ratio")?,
            cache_assertion_passed: passed.map(|c| c == 1.0),
            raw: Value::Null,
        },
        r.get("raw_json")?,
    ))
}

fn opt_num(v: Option<f64>) -> Sql {
    v.map_or(Sql::Null, Sql::Real)
}
fn opt_text(v: &Option<String>) -> Sql {
    v.clone().map_or(Sql::Null, Sql::Text)
}
fn opt_bool(v: Option<bool>) -> Sql {
    v.map_or(Sql::Null, |b| Sql::Integer(i64::from(b)))
}

/// Linear-interpolation percentile at rank `p·(n−1)` (`percentile`, `db/experiments.ts`).
pub fn percentile(values: &[f64], p: f64) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    if sorted.len() == 1 {
        return Some(sorted[0]);
    }
    let rank = p * (sorted.len() - 1) as f64;
    let lower = rank.floor();
    let upper = rank.ceil();
    if lower == upper {
        return Some(sorted[lower as usize]);
    }
    let weight = rank - lower;
    Some(sorted[lower as usize] * (1.0 - weight) + sorted[upper as usize] * weight)
}

fn numeric_flag(flags: &Value, key: &str) -> Option<f64> {
    flags.get(key).and_then(Value::as_f64)
}

fn string_flag(flags: &Value, key: &str) -> Option<String> {
    match flags.get(key) {
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Bool(b)) => Some(if *b { "on" } else { "off" }.into()),
        _ => None,
    }
}

/// `pickArm`: `production` -> `capability` -> the first arm inserted.
fn pick_arm<T>(by_arm: &[(Option<String>, T)]) -> Option<usize> {
    let find = |name: &str| by_arm.iter().position(|(k, _)| k.as_deref() == Some(name));
    find("production")
        .or_else(|| find("capability"))
        .or(if by_arm.is_empty() { None } else { Some(0) })
}

struct Round {
    decode: Option<f64>,
    prefill: Option<f64>,
}

impl Db {
    pub fn open(path: &str) -> Result<Db, String> {
        Ok(Db {
            conn: Mutex::new(open_database(path)?),
        })
    }

    pub fn conn(&self) -> MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// `replaceExperiment`: DELETE-then-INSERT of every row `record.id` owns, one transaction.
    pub fn replace_experiment(&self, r: &NormalizedRecord) -> DbResult<()> {
        let mut conn = self.conn();
        let tx = conn.transaction().map_err(e)?;
        tx.execute("DELETE FROM experiments WHERE id = ?", [&r.id])
            .map_err(e)?;
        tx.execute(
            "
        INSERT INTO experiments (
          id, schema_version, description, timestamp, hf_repo, hf_revision, filename,
          file_size, sha256, llama_cpp_build, sweep_cell_id, sweep_alias,
          sweep_server_flags, sweep_command, wilson_summary_json, sampling_arm,
          battery_json, vram_profile, model_host_path, raw_config_json, raw_prompts_json,
          raw_completions_json, ingested_at
        ) VALUES (?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ",
            params![
                r.id,
                opt_text(&r.description),
                opt_text(&r.timestamp),
                opt_text(&r.hf_repo),
                opt_text(&r.hf_revision),
                opt_text(&r.filename),
                opt_num(r.file_size),
                opt_text(&r.sha256),
                opt_text(&r.llama_cpp_build),
                opt_text(&r.sweep_cell_id),
                opt_text(&r.sweep_alias),
                opt_text(&r.sweep_server_flags.as_ref().map(stringify)),
                opt_text(&r.sweep_command),
                opt_text(&r.wilson_summary.as_ref().map(stringify)),
                opt_text(&r.sampling_arm),
                opt_text(
                    &r.battery
                        .as_ref()
                        .map(|b| stringify(&Value::from(b.clone())))
                ),
                opt_text(&r.vram_profile),
                opt_text(&r.model_host_path),
                stringify(&r.raw_config),
                stringify(&Value::Array(r.raw_prompts.clone())),
                stringify(&Value::Array(r.raw_completions.clone())),
                jsrt::iso_now(),
            ],
        )
        .map_err(e)?;
        {
            let mut insert = tx
                .prepare(
                    "
        INSERT INTO trials (
          experiment_id, line_index, type, trial, sampling_arm, temperature, top_p, top_k,
          sampler_seed, haystack_seed, corpus_manifest_id, haystack_sha256,
          classifier_verdict, classifier_shape, prompt_tokens, completion_tokens,
          finish_reason, position_fraction, task, cell_id, correct, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ",
                )
                .map_err(e)?;
            for t in &r.trials {
                insert
                    .execute(params![
                        r.id,
                        t.line_index as i64,
                        t.kind,
                        opt_num(t.trial),
                        opt_text(&t.sampling_arm),
                        opt_num(t.temperature),
                        opt_num(t.top_p),
                        opt_num(t.top_k),
                        opt_num(t.sampler_seed),
                        opt_num(t.haystack_seed),
                        opt_text(&t.corpus_manifest_id),
                        opt_text(&t.haystack_sha256),
                        opt_text(&t.classifier_verdict),
                        opt_text(&t.classifier_shape),
                        opt_num(t.prompt_tokens),
                        opt_num(t.completion_tokens),
                        opt_text(&t.finish_reason),
                        opt_num(t.position_fraction),
                        opt_text(&t.task),
                        opt_text(&t.cell_id),
                        opt_bool(t.correct),
                        stringify(&t.raw),
                    ])
                    .map_err(e)?;
            }
            let mut insert = tx
                .prepare(
                    "
        INSERT INTO metrics (
          experiment_id, line_index, metric_type, round_type, round_index, bytes,
          first_generation_token_ms, observed_decode_tok_s, prompt_tokens, cached_tokens,
          cache_hit_ratio, cache_assertion_passed, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ",
                )
                .map_err(e)?;
            for m in &r.metrics {
                insert
                    .execute(params![
                        r.id,
                        m.line_index.map(|i| i as i64),
                        m.metric_type,
                        opt_text(&m.round_type),
                        opt_num(m.round_index),
                        opt_num(m.bytes),
                        opt_num(m.first_generation_token_ms),
                        opt_num(m.observed_decode_tok_s),
                        opt_num(m.prompt_tokens),
                        opt_num(m.cached_tokens),
                        opt_num(m.cache_hit_ratio),
                        opt_bool(m.cache_assertion_passed),
                        stringify(&m.raw),
                    ])
                    .map_err(e)?;
            }
        }
        tx.execute("DELETE FROM summaries WHERE experiment_id = ?", [&r.id])
            .map_err(e)?;
        if let Some(w) = &r.wilson_summary {
            let num = |k: &str| opt_num(w.get(k).and_then(Value::as_f64));
            tx.execute(
                "INSERT INTO summaries (experiment_id, n, k, rate, sided, confidence, lower, upper, raw_json)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params![
                    r.id,
                    num("n"),
                    num("k"),
                    num("rate"),
                    w.get("sided").and_then(Value::as_str),
                    num("confidence"),
                    num("lower"),
                    num("upper"),
                    stringify(w),
                ],
            )
            .map_err(e)?;
        }
        tx.commit().map_err(e)
    }

    /// `markLegacyTimingProvenance` (`ingest/run.ts`).
    pub fn mark_legacy_timing_provenance(&self, id: &str) -> DbResult<()> {
        self.conn()
            .execute(
                "UPDATE experiments SET schema_origin = ? WHERE id = ?",
                params!["v1-timing", id],
            )
            .map(drop)
            .map_err(e)
    }

    /// `clearAllExperiments`.
    pub fn clear_all_experiments(&self) -> DbResult<()> {
        let mut conn = self.conn();
        let tx = conn.transaction().map_err(e)?;
        for table in ["summaries", "metrics", "trials", "experiments"] {
            tx.execute(&format!("DELETE FROM {table}"), []).map_err(e)?;
        }
        tx.commit().map_err(e)
    }

    pub fn list_experiment_summaries(&self) -> DbResult<Vec<ExperimentSummary>> {
        let conn = self.conn();
        let rows = conn
            .prepare(&format!(
                "SELECT {SUMMARY_COLUMNS} FROM experiments e ORDER BY e.timestamp DESC, e.id ASC"
            ))
            .map_err(e)?
            .query_map([], experiment_row)
            .map_err(e)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(e)?;
        rows.iter().map(row_to_summary).collect()
    }

    /// `getExperimentDetail` + `listTrials` + `listMetrics` (`routes/experiments.ts`).
    pub fn experiment_full_detail(&self, id: &str) -> DbResult<Option<ExperimentFullDetail>> {
        let row = self
            .conn()
            .query_row(
                &format!("SELECT {SUMMARY_COLUMNS} FROM experiments e WHERE e.id = ?"),
                [id],
                experiment_row,
            )
            .optional()
            .map_err(e)?;
        let Some(row) = row else {
            return Ok(None);
        };
        let wilson_summary = match truthy(&row.wilson_summary_json) {
            None => None,
            Some(s) => match validate::parse(&WILSON_SUMMARY.node, &parse(s)?) {
                Ok(Value::Null) => None,
                Ok(v) => Some(v),
                Err(issues) => {
                    return Err(format!("WilsonSummary: {}", validate::issues_json(&issues)))
                }
            },
        };
        let config = parse(&row.raw_config_json)?;
        let response_caps = config
            .get("response_caps")
            .filter(|v| v.is_object())
            .cloned();
        Ok(Some(ExperimentFullDetail {
            summary: row_to_summary(&row)?,
            wilson_summary,
            response_caps,
            trials: self.list_trials(id)?,
            metrics: self.list_metrics(id)?,
        }))
    }

    pub fn list_trials(&self, experiment_id: &str) -> DbResult<Vec<TrialRow>> {
        let conn = self.conn();
        let rows = conn
            .prepare(&format!(
                "SELECT {TRIAL_COLUMNS} FROM trials WHERE experiment_id = ? ORDER BY line_index ASC"
            ))
            .map_err(e)?
            .query_map([experiment_id], row_to_trial)
            .map_err(e)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(e)?;
        rows.into_iter()
            .map(|(mut t, raw)| {
                t.raw = parse(&raw)?;
                Ok(t)
            })
            .collect()
    }

    pub fn list_metrics(&self, experiment_id: &str) -> DbResult<Vec<MetricRow>> {
        let conn = self.conn();
        let rows = conn
            .prepare(&format!(
                "SELECT {METRIC_COLUMNS} FROM metrics WHERE experiment_id = ? ORDER BY id ASC"
            ))
            .map_err(e)?
            .query_map([experiment_id], row_to_metric)
            .map_err(e)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(e)?;
        rows.into_iter()
            .map(|(mut m, raw)| {
                m.raw = parse(&raw)?;
                Ok(m)
            })
            .collect()
    }

    pub fn experiment_count(&self) -> DbResult<f64> {
        self.conn()
            .query_row("SELECT COUNT(*) AS n FROM experiments", [], |r| r.get("n"))
            .map_err(e)
    }

    /// `listLadderRows`.
    pub fn list_ladder_rows(&self, hf_repo: &str) -> DbResult<Vec<LadderRow>> {
        let conn = self.conn();
        let rows = conn
            .prepare(&format!(
                "SELECT {SUMMARY_COLUMNS} FROM experiments e WHERE e.hf_repo = ? ORDER BY e.id ASC"
            ))
            .map_err(e)?
            .query_map([hf_repo], experiment_row)
            .map_err(e)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(e)?;
        let mut metric_q = conn
            .prepare("SELECT observed_decode_tok_s, first_generation_token_ms FROM metrics WHERE experiment_id = ? AND metric_type = 'timing_round'")
            .map_err(e)?;
        let mut trial_q = conn
            .prepare("SELECT correct FROM trials WHERE experiment_id = ?")
            .map_err(e)?;
        let mut out = Vec::new();
        for row in &rows {
            let flags = match truthy(&row.sweep_server_flags) {
                Some(s) => parse(s)?,
                None => Value::Object(Map::new()),
            };
            let metrics: Vec<(Option<f64>, Option<f64>)> = metric_q
                .query_map([&row.id], |r| Ok((r.get(0)?, r.get(1)?)))
                .map_err(e)?
                .collect::<Result<_, _>>()
                .map_err(e)?;
            let trials: Vec<Option<f64>> = trial_q
                .query_map([&row.id], |r| r.get(0))
                .map_err(e)?
                .collect::<Result<_, _>>()
                .map_err(e)?;
            let decode: Vec<f64> = metrics.iter().filter_map(|m| m.0).collect();
            let ttft: Vec<f64> = metrics.iter().filter_map(|m| m.1).collect();
            let scored: Vec<f64> = trials.iter().filter_map(|c| *c).collect();
            out.push(LadderRow {
                experiment_id: row.id.clone(),
                alias: row.sweep_alias.clone(),
                cell_id: row.sweep_cell_id.clone(),
                filename: row.filename.clone(),
                context_length: numeric_flag(&flags, "-c"),
                kv_cache_type_k: string_flag(&flags, "-ctk"),
                kv_cache_type_v: string_flag(&flags, "-ctv"),
                flash_attention: string_flag(&flags, "-fa"),
                median_decode_tok_s: percentile(&decode, 0.5),
                median_first_generation_token_ms: percentile(&ttft, 0.5),
                correctness_rate: if scored.is_empty() {
                    None
                } else {
                    Some(scored.iter().filter(|c| **c == 1.0).count() as f64 / scored.len() as f64)
                },
                trial_count: trials.len() as f64,
            });
        }
        Ok(out)
    }

    /// `listServingTimingRows`.
    pub fn list_serving_timing_rows(
        &self,
        served: &[ServedModel],
    ) -> DbResult<Vec<ServingTimingRow>> {
        struct Source {
            row: ResolvableTimingRow,
            sampling_arm: Option<String>,
            decode: Option<f64>,
            prefill: Option<f64>,
        }
        let conn = self.conn();
        let sources: Vec<Source> = conn
            .prepare(
                "
        SELECT
          e.model_host_path AS model_host_path,
          e.sweep_alias AS sweep_alias,
          e.schema_origin AS schema_origin,
          e.sampling_arm AS sampling_arm,
          e.sweep_server_flags AS sweep_server_flags,
          m.observed_decode_tok_s AS observed_decode_tok_s,
          json_extract(m.raw_json, '$.server_prompt_eval_tok_s') AS observed_prefill_tok_s,
          json_type(m.raw_json, '$.server_prompt_eval_tok_s') AS observed_prefill_json_type
        FROM metrics m
        JOIN experiments e ON e.id = m.experiment_id
        WHERE m.metric_type = 'timing_round'
      ",
            )
            .map_err(e)?
            .query_map([], |r| {
                let json_type: Option<String> = r.get("observed_prefill_json_type")?;
                let raw: Sql = r.get("observed_prefill_tok_s")?;
                // `prefillSample`: only a JSON integer/real admitted, finite.
                let prefill = match (json_type.as_deref(), raw) {
                    (Some("integer" | "real"), Sql::Integer(i)) => Some(i as f64),
                    (Some("integer" | "real"), Sql::Real(f)) if f.is_finite() => Some(f),
                    _ => None,
                };
                Ok(Source {
                    row: ResolvableTimingRow {
                        model_host_path: r.get("model_host_path")?,
                        sweep_alias: r.get("sweep_alias")?,
                        schema_origin: r.get("schema_origin")?,
                        sweep_server_flags: r.get("sweep_server_flags")?,
                    },
                    sampling_arm: r.get("sampling_arm")?,
                    decode: r.get("observed_decode_tok_s")?,
                    prefill,
                })
            })
            .map_err(e)?
            .collect::<Result<_, _>>()
            .map_err(e)?;

        struct Group {
            model_key: String,
            aliases: Vec<String>,
            mtp: bool,
            by_arm: Vec<(Option<String>, Vec<Round>)>,
        }
        let mut groups: Vec<Group> = Vec::new();
        for s in sources {
            let Some(resolved) = resolve_served_model(&s.row, served) else {
                continue;
            };
            if !resolved.recipe_match {
                continue;
            }
            let gi = match groups
                .iter()
                .position(|g| g.model_key == resolved.model_key)
            {
                Some(i) => i,
                None => {
                    groups.push(Group {
                        model_key: resolved.model_key.clone(),
                        aliases: resolved.aliases.clone(),
                        mtp: resolved.mtp,
                        by_arm: Vec::new(),
                    });
                    groups.len() - 1
                }
            };
            let round = Round {
                decode: s.decode,
                prefill: s.prefill,
            };
            let g = &mut groups[gi];
            match g.by_arm.iter_mut().find(|(k, _)| *k == s.sampling_arm) {
                Some((_, rounds)) => rounds.push(round),
                None => g.by_arm.push((s.sampling_arm, vec![round])),
            }
        }
        let mut rows = Vec::new();
        for g in groups {
            let Some(ai) = pick_arm(&g.by_arm) else {
                continue;
            };
            let (arm, rounds) = &g.by_arm[ai];
            if rounds.is_empty() {
                continue;
            }
            let decode: Vec<f64> = rounds.iter().filter_map(|r| r.decode).collect();
            let prefill: Vec<f64> = rounds.iter().filter_map(|r| r.prefill).collect();
            rows.push(ServingTimingRow {
                model_key: g.model_key.clone(),
                aliases: g.aliases.clone(),
                p50_prefill_tok_s: percentile(&prefill, 0.5),
                p90_prefill_tok_s: percentile(&prefill, 0.9),
                p50_decode_tok_s: percentile(&decode, 0.5),
                p90_decode_tok_s: percentile(&decode, 0.9),
                sampling_arm: arm.clone(),
                sample_count: rounds.len() as f64,
                mtp: g.mtp,
            });
        }
        rows.sort_by(|a, b| jsrt::locale_compare(&a.model_key, &b.model_key));
        Ok(rows)
    }

    /// `listLongcontextTrials`.
    pub fn list_longcontext_trials(&self, experiment_id: &str) -> DbResult<Vec<CurveTrial>> {
        let conn = self.conn();
        let rows = conn
            .prepare("SELECT task, position_fraction, classifier_verdict FROM trials WHERE experiment_id = ? AND type = 'longcontext_probe' ORDER BY line_index ASC")
            .map_err(e)?
            .query_map([experiment_id], |r| {
                Ok(CurveTrial {
                    task: r.get(0)?,
                    position_fraction: r.get(1)?,
                    classifier_verdict: r.get(2)?,
                })
            })
            .map_err(e)?
            .collect::<Result<_, _>>()
            .map_err(e)?;
        Ok(rows)
    }

    /// `getCaptureRecord`.
    pub fn capture_record(
        &self,
        experiment_id: &str,
        served: &[ServedModel],
    ) -> DbResult<Option<CaptureRecord>> {
        let conn = self.conn();
        type Head = (String, String, String, String, ResolvableTimingRow);
        let head: Option<Head> = conn
            .query_row(
                "SELECT id, raw_config_json, raw_prompts_json, raw_completions_json, model_host_path, sweep_alias, schema_origin, sweep_server_flags FROM experiments WHERE id = ?",
                [experiment_id],
                |r| {
                    Ok((
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        ResolvableTimingRow {
                            model_host_path: r.get(4)?,
                            sweep_alias: r.get(5)?,
                            schema_origin: r.get(6)?,
                            sweep_server_flags: r.get(7)?,
                        },
                    ))
                },
            )
            .optional()
            .map_err(e)?;
        let Some((id, config, prompts, completions, resolvable)) = head else {
            return Ok(None);
        };
        let lines = |sql: &str| -> DbResult<Vec<(f64, String)>> {
            conn.prepare(sql)
                .map_err(e)?
                .query_map([experiment_id], |r| Ok((r.get(0)?, r.get(1)?)))
                .map_err(e)?
                .collect::<Result<_, _>>()
                .map_err(e)
        };
        let mut entries = lines("SELECT line_index, raw_json FROM trials WHERE experiment_id = ? ORDER BY line_index ASC")?;
        entries.extend(lines("SELECT line_index, raw_json FROM metrics WHERE experiment_id = ? AND line_index IS NOT NULL")?);
        entries.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
        let samples = entries
            .iter()
            .map(|(_, raw)| parse(raw))
            .collect::<DbResult<Vec<_>>>()?;
        Ok(Some(CaptureRecord {
            experiment_id: id,
            config: parse(&config)?,
            prompts: parse(&prompts)?,
            completions: parse(&completions)?,
            samples,
            serving: resolve_served_model(&resolvable, served),
        }))
    }

    pub fn list_experiment_ids(&self) -> DbResult<Vec<String>> {
        let conn = self.conn();
        let ids = conn
            .prepare("SELECT id FROM experiments ORDER BY id ASC")
            .map_err(e)?
            .query_map([], |r| r.get(0))
            .map_err(e)?
            .collect::<Result<_, _>>()
            .map_err(e)?;
        Ok(ids)
    }
}

#[cfg(test)]
#[path = "db_tests.rs"]
mod tests;
