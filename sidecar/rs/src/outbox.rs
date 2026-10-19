//! outbox.mjs — the sidecar's WRITE half of the tracker event seam: rows appended to JSONL
//! day-files shared with the Claude Code hooks (`fleet-tools/scripts/outbox.mjs` copy).
//!
//! Invariants (see the Node file): pinned, byte-identical line format; one day-file per UTC date
//! of the row's `ts` under `<dirname(path)>/log/`; dir 0700, files 0600; one append per row; a
//! row over `LINE_MAX_BYTES` dropped whole; no message content; fail-open; an empty path opens
//! nothing.

use crate::js;
use serde_json::{Map, Value};
use std::collections::hash_map::RandomState;
use std::fs::{self, OpenOptions};
use std::hash::{BuildHasher, Hasher};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

pub const KINDS: &[&str] = &["turn.completed", "usage", "rate_limit", "child.spawned"];
pub const ROW_VERSION: i64 = 1;
pub const PRUNE_AFTER_MS: f64 = 7.0 * 24.0 * 60.0 * 60.0 * 1000.0;
pub const LINE_MAX_BYTES: usize = 64 * 1024;
const PRUNE_ODDS: u64 = 50;
const DAY_MS: f64 = 86_400_000.0;

pub fn log_dir_for(path: &str) -> PathBuf {
    Path::new(path)
        .parent()
        .unwrap_or(Path::new("."))
        .join("log")
}

fn text(v: Option<&Value>) -> Value {
    match v {
        Some(Value::String(s)) if !s.is_empty() => Value::from(s.as_str()),
        _ => Value::Null,
    }
}

/// `serializeRow(event)`: the pinned line format, trailing newline included. A key the event
/// lacks (`session_id`, `kind`) is omitted, as `JSON.stringify` omits `undefined`.
pub fn serialize_row(event: &Value) -> String {
    let g = |k: &str| js::prop(Some(event), k);
    let mut row = Map::new();
    row.insert("v".into(), Value::from(ROW_VERSION));
    let ts = match text(g("ts")) {
        Value::Null => Value::from(js::iso_now()),
        t => t,
    };
    row.insert("ts".into(), ts);
    row.insert("harness".into(), Value::from("codex-sidecar"));
    row.insert("leg".into(), text(g("leg")));
    if let Some(v) = g("session_id") {
        row.insert("session_id".into(), v.clone());
    }
    row.insert("agent_id".into(), text(g("agent_id")));
    row.insert("agent_type".into(), text(g("agent_type")));
    if let Some(v) = g("kind") {
        row.insert("kind".into(), v.clone());
    }
    row.insert(
        "payload".into(),
        js::present(g("payload"))
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new())),
    );
    format!("{}\n", js::stringify(&Value::Object(row)))
}

/// `openOutbox(path)`: creates the log dir (0700) if needed.
pub fn open_outbox(path: &str) -> std::io::Result<PathBuf> {
    let dir = log_dir_for(path);
    let existed = dir.is_dir();
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&dir)?;
    if !existed {
        let _ = fs::set_permissions(&dir, fs::Permissions::from_mode(0o700));
    }
    Ok(dir)
}

fn day_of(name: &str) -> Option<f64> {
    let day = name.strip_suffix(".jsonl")?;
    if !crate::logging::is_day_shape(day) {
        return None;
    }
    js::date_parse(&format!("{day}T00:00:00Z"))
}

/// `prune(logDir, {now, olderThanMs})`: unlinks day-files older than retention + 1 day.
pub fn prune(log_dir: &Path, now_ms: f64, older_than_ms: f64) -> std::io::Result<usize> {
    let cutoff = now_ms - older_than_ms - DAY_MS;
    let mut removed = 0;
    for e in fs::read_dir(log_dir)? {
        let Ok(e) = e else { continue };
        let name = e.file_name().to_string_lossy().into_owned();
        if !day_of(&name).is_some_and(|d| d < cutoff) {
            continue;
        }
        if fs::remove_file(log_dir.join(&name)).is_ok() {
            removed += 1;
        }
    }
    Ok(removed)
}

/// One writer per process. An empty `path` makes every call a no-op.
#[derive(Debug, Default)]
pub struct OutboxWriter {
    path: String,
    log_dir: Option<PathBuf>,
}

impl OutboxWriter {
    pub fn new(path: &str) -> Self {
        Self {
            path: path.to_string(),
            log_dir: None,
        }
    }

    pub fn path(&self) -> &str {
        &self.path
    }

    /// `record(event)`: the day-file written, or `None` when the row was dropped (fail-open).
    pub fn record(&mut self, event: &Value) -> Option<PathBuf> {
        if self.path.is_empty() {
            return None;
        }
        let kind = js::prop(Some(event), "kind").and_then(Value::as_str)?;
        if !KINDS.contains(&kind) {
            return None;
        }
        let sid = js::prop(Some(event), "session_id").and_then(Value::as_str)?;
        if sid.is_empty() {
            return None;
        }
        let line = serialize_row(event);
        if line.len() > LINE_MAX_BYTES {
            return None;
        }
        if self.log_dir.is_none() {
            self.log_dir = Some(open_outbox(&self.path).ok()?);
        }
        let log_dir = self.log_dir.clone()?;
        let ts: Value = serde_json::from_str(&line).ok()?;
        let ms = js::prop(Some(&ts), "ts")
            .and_then(Value::as_str)
            .and_then(js::date_parse)
            .unwrap_or_else(js::now_ms);
        let day = js::iso_from_ms(ms)[..10].to_string();
        let file = log_dir.join(format!("{day}.jsonl"));
        let mut fd = OpenOptions::new()
            .append(true)
            .create(true)
            .mode(0o600)
            .open(&file)
            .ok()?;
        fd.write_all(line.as_bytes()).ok()?;
        drop(fd);
        if RandomState::new().build_hasher().finish() % PRUNE_ODDS == 0 {
            let _ = prune(&log_dir, js::now_ms(), PRUNE_AFTER_MS);
        }
        Some(file)
    }
}
