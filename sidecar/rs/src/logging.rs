//! logging.mjs — the durable JSONL request log.
//!
//! One day-rolled file per process under a 0700 directory, each file 0600, pruned past the
//! retention window. `write` is deliberately total: logging must never break a request, so
//! every failure here is swallowed. `redact_headers` is the one rule about what may be written.

use crate::js;
use serde_json::{Map, Value};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

pub struct Logger {
    pub dir: PathBuf,
    pub keep_days: f64,
    day: Option<String>,
    fd: Option<File>,
    pub file: Option<PathBuf>,
}

impl Logger {
    pub fn new(dir: impl Into<PathBuf>, keep_days: f64) -> Self {
        Self {
            dir: dir.into(),
            keep_days,
            day: None,
            fd: None,
            file: None,
        }
    }

    /// `Logger.dayOf()` — today's UTC `YYYY-MM-DD`.
    pub fn day_of() -> String {
        js::iso_now()[..10].to_string()
    }

    fn rollover(&mut self) -> std::io::Result<()> {
        let day = Self::day_of();
        if self.fd.is_some() && self.day.as_deref() == Some(day.as_str()) {
            return Ok(());
        }
        self.fd = None;
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.dir)?;
        let _ = fs::set_permissions(&self.dir, fs::Permissions::from_mode(0o700));
        let file = self.dir.join(format!("codex-sidecar-{day}.jsonl"));
        let fd = OpenOptions::new()
            .append(true)
            .create(true)
            .mode(0o600)
            .open(&file)?;
        let _ = fs::set_permissions(&file, fs::Permissions::from_mode(0o600));
        self.fd = Some(fd);
        self.day = Some(day);
        self.file = Some(file);
        self.prune(js::now_ms());
        Ok(())
    }

    /// Unlink day-files older than `keep_days`, never the open one.
    pub fn prune(&self, now_ms: f64) {
        let cutoff = now_ms - self.keep_days * 86_400_000.0;
        let Ok(rd) = fs::read_dir(&self.dir) else {
            return;
        };
        let current = self
            .file
            .as_deref()
            .and_then(Path::file_name)
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        for e in rd.flatten() {
            let n = e.file_name().to_string_lossy().into_owned();
            let Some(day) = n
                .strip_prefix("codex-sidecar-")
                .and_then(|r| r.strip_suffix(".jsonl"))
            else {
                continue;
            };
            if !is_day_shape(day) || n == current {
                continue;
            }
            if let Some(t) = js::date_parse(&format!("{day}T00:00:00Z")) {
                if t < cutoff {
                    let _ = fs::remove_file(self.dir.join(&n));
                }
            }
        }
    }

    /// One row: `{ts, kind, ...obj}` (an `obj` key named `ts`/`kind` overrides in place, as the
    /// JS spread does). Never fails.
    pub fn write(&mut self, kind: &str, obj: &Map<String, Value>) {
        let mut row = Map::new();
        row.insert("ts".into(), Value::from(js::iso_now()));
        row.insert("kind".into(), Value::from(kind));
        for (k, v) in obj {
            row.insert(k.clone(), v.clone());
        }
        let line = format!("{}\n", js::stringify(&Value::Object(row)));
        if self.rollover().is_err() {
            return;
        }
        if let Some(fd) = self.fd.as_mut() {
            let _ = fd.write_all(line.as_bytes());
        }
    }

    pub fn close(&mut self) {
        self.fd = None;
    }
}

/// `\d{4}-\d{2}-\d{2}`
pub(crate) fn is_day_shape(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 10
        && b[4] == b'-'
        && b[7] == b'-'
        && b.iter()
            .enumerate()
            .all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit())
}

/// `redactHeaders(headers)`: the four credential headers become `<redacted>`, order kept.
pub fn redact_headers(headers: &Map<String, Value>) -> Map<String, Value> {
    headers
        .iter()
        .map(|(k, v)| {
            let secret = matches!(
                k.to_ascii_lowercase().as_str(),
                "authorization" | "x-api-key" | "cookie" | "proxy-authorization"
            );
            let v = if secret {
                Value::from("<redacted>")
            } else {
                v.clone()
            };
            (k.clone(), v)
        })
        .collect()
}
