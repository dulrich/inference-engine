//! `apps/api/src/routes/queue.ts`: `GET /api/queue` — every `*.json` queue file (not
//! `*.state.json`) joined against its `<stem>.state.json` by alias, a malformed file reported as
//! that one entry's `error`, the per-queue ETA (`eta.rs`), and the `control` section projected
//! from `<CONTROL_HOST_DIR>/status.json` with its read-time 180 s staleness.
use crate::eta::{compute_eta_for_queue, CellDescriptor, QueueEta};
use crate::jsrt;
use bd_domain::contract::{CELL_STATE_VALUES, CONTROL_SECTION, QUEUE_STATE_VALUES};
use bd_domain::validate;
use serde::Serialize;
use serde_json::{Map, Value};

/// 3x the minutely dispatcher period.
pub const CONTROL_STALE_THRESHOLD_MS: f64 = 180_000.0;

fn as_string(v: Option<&Value>) -> Option<String> {
    v.and_then(Value::as_str).map(str::to_string)
}

fn as_enum(v: Option<&Value>, values: &[&'static str]) -> Option<&'static str> {
    let s = v.and_then(Value::as_str)?;
    values.iter().copied().find(|x| *x == s)
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct QueueCell {
    pub alias: String,
    pub model: Option<String>,
    pub c: Option<f64>,
    pub ctk: Option<String>,
    pub ctv: Option<String>,
    pub fa: Option<String>,
    pub sampling_arm: Option<String>,
    pub trials: Option<f64>,
    pub state: Option<&'static str>,
    pub reason: Option<String>,
}

struct QueueFile {
    deadline: Option<String>,
    vram_profile: Option<String>,
    cells: Vec<QueueCell>,
}

fn parse_queue_file(raw: &Value) -> Result<QueueFile, String> {
    let Value::Object(m) = raw else {
        return Err("queue file is not a JSON object".into());
    };
    let Some(cells) = m.get("cells").and_then(Value::as_array) else {
        return Err("queue file 'cells' is not an array".into());
    };
    let cells = cells
        .iter()
        .filter_map(|c| {
            let c = c.as_object()?;
            Some(QueueCell {
                alias: as_string(c.get("alias"))?,
                model: as_string(c.get("model")),
                c: jsrt::as_number(c.get("c")),
                ctk: as_string(c.get("ctk")),
                ctv: as_string(c.get("ctv")),
                fa: as_string(c.get("fa")),
                sampling_arm: as_string(c.get("sampling_arm")),
                trials: jsrt::as_number(c.get("trials")),
                state: None,
                reason: None,
            })
        })
        .collect();
    Ok(QueueFile {
        deadline: as_string(m.get("deadline")),
        vram_profile: as_string(m.get("vram_profile")),
        cells,
    })
}

struct StateFile {
    queue_state: Option<&'static str>,
    /// alias -> (state, reason), last entry wins.
    by_alias: Vec<(String, Option<&'static str>, Option<String>)>,
    counts: Vec<f64>,
}

fn parse_state_file(raw: &Value) -> Result<StateFile, String> {
    let Value::Object(m) = raw else {
        return Err("state file is not a JSON object".into());
    };
    let Some(Value::Object(cells)) = m.get("cells") else {
        return Err("state file 'cells' is not an object".into());
    };
    let mut counts = vec![0.0; CELL_STATE_VALUES.len()];
    let mut by_alias: Vec<(String, Option<&'static str>, Option<String>)> = Vec::new();
    for (_, entry) in jsrt::js_entries(cells) {
        let Value::Object(e) = entry else { continue };
        let state = as_enum(e.get("state"), CELL_STATE_VALUES);
        if let Some(s) = state {
            if let Some(i) = CELL_STATE_VALUES.iter().position(|x| *x == s) {
                counts[i] += 1.0;
            }
        }
        let reason = as_string(e.get("reason"));
        let detail_alias = e
            .get("detail")
            .and_then(Value::as_object)
            .and_then(|d| as_string(d.get("alias")));
        if let Some(alias) = as_string(e.get("alias")).or(detail_alias) {
            match by_alias.iter_mut().find(|(a, _, _)| *a == alias) {
                Some(slot) => {
                    slot.1 = state;
                    slot.2 = reason;
                }
                None => by_alias.push((alias, state, reason)),
            }
        }
    }
    Ok(StateFile {
        queue_state: as_enum(m.get("queue_state"), QUEUE_STATE_VALUES),
        by_alias,
        counts,
    })
}

fn read_json_file(path: &str) -> Result<Value, String> {
    let bytes = std::fs::read(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            format!("ENOENT: no such file or directory, open '{path}'")
        } else {
            format!("{e}, open '{path}'")
        }
    })?;
    jsrt::json_parse_js(&String::from_utf8_lossy(&bytes))
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct QueueEntry {
    pub name: String,
    pub queue_state: Option<&'static str>,
    pub deadline: Option<String>,
    pub vram_profile: Option<String>,
    pub cell_counts: Map<String, Value>,
    pub cells: Vec<QueueCell>,
    pub eta: QueueEta,
}

#[derive(Debug, Serialize)]
pub struct QueueErrorEntry {
    pub name: String,
    pub error: String,
    pub eta: Option<()>,
}

#[derive(Debug, Serialize)]
#[serde(untagged)]
pub enum QueueListItem {
    Entry(Box<QueueEntry>),
    Error(QueueErrorEntry),
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct QueueListResponse {
    pub queues: Vec<QueueListItem>,
    pub generated_at: String,
    pub control: Value,
}

fn counts_map(counts: Option<&[f64]>) -> Map<String, Value> {
    CELL_STATE_VALUES
        .iter()
        .enumerate()
        .map(|(i, s)| {
            let v = counts.map_or(Value::Null, |c| bd_domain::js::json_number(c[i]));
            ((*s).to_string(), v)
        })
        .collect()
}

fn build_entry(
    queues_dir: &str,
    stem: &str,
    file_name: &str,
    records_dir: Option<&str>,
    control: &Value,
) -> Result<QueueEntry, String> {
    let queue = parse_queue_file(&read_json_file(&jsrt::path_join(queues_dir, file_name))?)?;
    let state_path = jsrt::path_join(queues_dir, &format!("{stem}.state.json"));
    let state = if std::path::Path::new(&state_path).is_file() {
        Some(parse_state_file(&read_json_file(&state_path)?)?)
    } else {
        None
    };
    let cells: Vec<QueueCell> = queue
        .cells
        .into_iter()
        .map(|mut cell| {
            if let Some((_, st, reason)) = state
                .as_ref()
                .and_then(|s| s.by_alias.iter().find(|(a, _, _)| *a == cell.alias))
            {
                cell.state = *st;
                cell.reason = reason.clone();
            }
            cell
        })
        .collect();
    let counts = state.as_ref().map(|s| s.counts.as_slice());
    let remaining = counts.map(|c| c[0] + c[1]);
    let live_started_at = control
        .get("live")
        .filter(|l| l.get("queueStem").and_then(Value::as_str) == Some(stem))
        .and_then(|l| l.get("startedAt"))
        .and_then(Value::as_str);
    let descriptors: Vec<CellDescriptor> = cells
        .iter()
        .map(|c| CellDescriptor {
            model: c.model.clone(),
            c: c.c,
            state: c.state.map(str::to_string),
        })
        .collect();
    let eta = compute_eta_for_queue(records_dir, stem, remaining, live_started_at, &descriptors);
    Ok(QueueEntry {
        name: stem.to_string(),
        queue_state: state.as_ref().and_then(|s| s.queue_state),
        deadline: queue.deadline,
        vram_profile: queue.vram_profile,
        cell_counts: counts_map(counts),
        cells,
        eta,
    })
}

/// `buildQueueListResponse(queuesDir, recordsDir, control)` + the route's `{...response,
/// control}`.
pub fn build_queue_list_response(
    queues_dir: &str,
    records_dir: Option<&str>,
    control: Value,
) -> QueueListResponse {
    let generated_at = jsrt::iso_now();
    let names: Option<Vec<String>> = std::fs::read_dir(queues_dir).ok().map(|rd| {
        let mut v: Vec<String> = rd
            .filter_map(Result::ok)
            .filter(|e| e.file_type().is_ok_and(|t| t.is_file()))
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.ends_with(".json") && !n.ends_with(".state.json"))
            .collect();
        v.sort_by(|a, b| jsrt::utf16_cmp(a, b));
        v
    });
    let Some(names) = names else {
        return QueueListResponse {
            queues: Vec::new(),
            generated_at,
            control,
        };
    };
    let queues = names
        .iter()
        .map(|file_name| {
            let stem = &file_name[..file_name.len() - ".json".len()];
            match build_entry(queues_dir, stem, file_name, records_dir, &control) {
                Ok(e) => QueueListItem::Entry(Box::new(e)),
                Err(error) => QueueListItem::Error(QueueErrorEntry {
                    name: stem.to_string(),
                    error,
                    eta: None,
                }),
            }
        })
        .collect();
    QueueListResponse {
        queues,
        generated_at,
        control,
    }
}

/// Inserts `key: value` only when the source key is present (a JS `undefined` property is
/// absent on the candidate, which the schema then rejects or skips).
fn put(out: &mut Map<String, Value>, key: &str, v: Option<&Value>) {
    if let Some(v) = v {
        out.insert(key.into(), v.clone());
    }
}

fn project_live(raw: Option<&Value>) -> Value {
    match raw {
        None | Some(Value::Null) => Value::Null,
        Some(Value::Object(r)) => {
            let mut o = Map::new();
            put(&mut o, "requestId", r.get("request_id"));
            put(&mut o, "queueStem", r.get("queue_stem"));
            put(&mut o, "phase", r.get("phase"));
            put(&mut o, "startedAt", r.get("started_at"));
            Value::Object(o)
        }
        Some(other) => other.clone(),
    }
}

fn project_last_run(raw: Option<&Value>) -> Value {
    match raw {
        None | Some(Value::Null) => Value::Null,
        Some(Value::Object(r)) => {
            let mut o = Map::new();
            put(&mut o, "requestId", r.get("request_id"));
            put(&mut o, "endedAt", r.get("ended_at"));
            put(&mut o, "runExit", r.get("run_exit"));
            put(&mut o, "verifyExit", r.get("verify_exit"));
            o.insert(
                "restartFailed".into(),
                r.get("restart_failed").cloned().unwrap_or(Value::Null),
            );
            put(&mut o, "disposition", r.get("disposition"));
            Value::Object(o)
        }
        Some(other) => other.clone(),
    }
}

fn project_digest(raw: &Value) -> Value {
    let Value::Object(r) = raw else {
        return raw.clone();
    };
    let mut o = Map::new();
    put(&mut o, "requestId", r.get("request_id"));
    put(&mut o, "action", r.get("action"));
    put(&mut o, "state", r.get("state"));
    put(&mut o, "reason", r.get("reason"));
    put(&mut o, "requestedAt", r.get("requested_at"));
    Value::Object(o)
}

/// `buildControlSection(controlHostDir)` with an injected clock; `null` on every failure.
pub fn build_control_section(control_host_dir: Option<&str>, now_ms: f64) -> Value {
    let Some(dir) = control_host_dir.filter(|d| !d.is_empty()) else {
        return Value::Null;
    };
    let Ok(Value::Object(raw)) = read_json_file(&jsrt::path_join(dir, "status.json")) else {
        return Value::Null;
    };
    let generated_ms = raw
        .get("generated_at")
        .and_then(Value::as_str)
        .and_then(jsrt::date_parse);
    let stale = generated_ms.is_none_or(|ms| now_ms - ms > CONTROL_STALE_THRESHOLD_MS);
    let mut candidate = Map::new();
    put(&mut candidate, "statusGeneratedAt", raw.get("generated_at"));
    candidate.insert("stale".into(), Value::Bool(stale));
    put(&mut candidate, "armed", raw.get("armed"));
    candidate.insert("live".into(), project_live(raw.get("live")));
    candidate.insert("lastRun".into(), project_last_run(raw.get("last_run")));
    put(&mut candidate, "anomalies", raw.get("anomalies"));
    match raw.get("requests") {
        Some(Value::Array(items)) => {
            candidate.insert(
                "requests".into(),
                Value::Array(items.iter().map(project_digest).collect()),
            );
        }
        other => put(&mut candidate, "requests", other),
    }
    validate::parse(&CONTROL_SECTION.node, &Value::Object(candidate)).unwrap_or(Value::Null)
}

#[cfg(test)]
#[path = "queue_tests.rs"]
mod tests;
