//! The sidecar's WRITE half of the tracker event seam: rows appended to JSONL
//! day-files shared with the Claude Code hooks (`fleet-tools/scripts/outbox.mjs` copy).
//!
//! Invariants (shared with that writer): pinned, byte-identical line format; one day-file per UTC date
//! of the row's `ts` under `<dirname(path)>/log/`; dir 0700, files 0600; one append per row; a
//! row over `LINE_MAX_BYTES` dropped whole; no message content; fail-open; an empty path opens
//! nothing.

use serde::Serialize;
use serde_json::Value;
use std::fs::{self, OpenOptions};
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

fn text(v: Option<&Value>) -> Option<&str> {
    v.and_then(Value::as_str).filter(|s| !s.is_empty())
}

/// The pinned line, in its byte order (struct field order is the key order; `None` is `null`
/// except where skipped, i.e. omitted).
#[derive(Serialize)]
struct Line<'a, S: Serialize, K: Serialize, P: Serialize> {
    v: i64,
    ts: &'a str,
    harness: &'static str,
    leg: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    session_id: Option<S>,
    agent_id: Option<&'a str>,
    agent_type: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    kind: Option<K>,
    payload: P,
}

impl<S: Serialize, K: Serialize, P: Serialize> Line<'_, S, K, P> {
    fn to_line(&self) -> String {
        format!("{}\n", serde_json::to_string(self).unwrap_or_default())
    }
}

/// One typed outbox row (the sidecar's own writes). `harness` is always `codex-sidecar`
/// and `agent_type` always `null`; `ts` is stamped at write time.
pub struct Row<'a, P: Serialize> {
    pub leg: &'a str,
    pub session_id: &'a str,
    pub agent_id: Option<&'a str>,
    pub kind: &'static str,
    pub payload: P,
}

/// The pinned line format, trailing newline included, and the `ts`
/// written. A key the event lacks (`session_id`, `kind`) is omitted.
fn serialize_value(event: &Value) -> (String, String) {
    let g = |k: &str| event.get(k);
    let ts = text(g("ts")).map_or_else(crate::logging::iso_now, str::to_string);
    let empty = Value::Object(Default::default());
    let line = Line {
        v: ROW_VERSION,
        ts: &ts,
        harness: "codex-sidecar",
        leg: text(g("leg")),
        session_id: g("session_id"),
        agent_id: text(g("agent_id")),
        agent_type: text(g("agent_type")),
        kind: g("kind"),
        payload: g("payload").filter(|p| !p.is_null()).unwrap_or(&empty),
    }
    .to_line();
    (ts, line)
}

/// The pinned line format, trailing newline included.
pub fn serialize_row(event: &Value) -> String {
    serialize_value(event).1
}

/// Opens the outbox; creates the log dir (0700) if needed.
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
    crate::logging::day_ms(name.strip_suffix(".jsonl")?)
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
        let kind = event.get("kind").and_then(Value::as_str)?;
        if !KINDS.contains(&kind) {
            return None;
        }
        let sid = event.get("session_id").and_then(Value::as_str)?;
        if sid.is_empty() {
            return None;
        }
        let (ts, line) = serialize_value(event);
        self.append(&ts, &line)
    }

    /// A typed row; the same line bytes as [`record`](Self::record) of the equivalent event.
    pub fn record_row<P: Serialize>(&mut self, row: &Row<'_, P>) -> Option<PathBuf> {
        if self.path.is_empty() || row.session_id.is_empty() || !KINDS.contains(&row.kind) {
            return None;
        }
        let ts = crate::logging::iso_now();
        let line = Line {
            v: ROW_VERSION,
            ts: &ts,
            harness: "codex-sidecar",
            leg: Some(row.leg).filter(|l| !l.is_empty()),
            session_id: Some(row.session_id),
            agent_id: row.agent_id.filter(|a| !a.is_empty()),
            agent_type: None,
            kind: Some(row.kind),
            payload: &row.payload,
        }
        .to_line();
        self.append(&ts, &line)
    }

    fn append(&mut self, ts: &str, line: &str) -> Option<PathBuf> {
        if line.len() > LINE_MAX_BYTES {
            return None;
        }
        if self.log_dir.is_none() {
            self.log_dir = Some(open_outbox(&self.path).ok()?);
        }
        let log_dir = self.log_dir.clone()?;
        // the row's own UTC day when its `ts` opens with one, else today
        let day = ts
            .get(..10)
            .filter(|d| crate::logging::day_ms(d).is_some())
            .map_or_else(
                || crate::logging::iso_now()[..10].to_string(),
                str::to_string,
            );
        let file = log_dir.join(format!("{day}.jsonl"));
        let mut fd = OpenOptions::new()
            .append(true)
            .create(true)
            .mode(0o600)
            .open(&file)
            .ok()?;
        fd.write_all(line.as_bytes()).ok()?;
        drop(fd);
        let draw = crate::sidecar::random_bytes(8)
            .ok()
            .map(|b| u64::from_le_bytes(b.try_into().unwrap_or_default()));
        if draw.is_some_and(|d| d % PRUNE_ODDS == 0) {
            let _ = prune(&log_dir, crate::logging::now_ms(), PRUNE_AFTER_MS);
        }
        Some(file)
    }
}
