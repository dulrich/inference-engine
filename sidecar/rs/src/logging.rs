//! The durable JSONL request log.
//!
//! One day-rolled file per process under a 0700 directory, each file 0600, pruned past the
//! retention window. `write` is deliberately total: logging must never break a request, so
//! every failure here is swallowed. `redact_headers` is the one rule about what may be written.

use axum::http::HeaderMap;
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

    /// Today's UTC `YYYY-MM-DD`.
    pub fn day_of() -> String {
        iso_now()[..10].to_string()
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
        self.prune(now_ms());
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
            if n == current {
                continue;
            }
            if let Some(t) = day_ms(day) {
                if t < cutoff {
                    let _ = fs::remove_file(self.dir.join(&n));
                }
            }
        }
    }

    /// One row: `{ts, kind, ...obj}` (an `obj` key named `ts`/`kind` overrides in place, keeping its
    /// position). Never fails.
    pub fn write(&mut self, kind: &str, obj: &Map<String, Value>) {
        let mut row = Map::new();
        row.insert("ts".into(), Value::from(iso_now()));
        row.insert("kind".into(), Value::from(kind));
        for (k, v) in obj {
            row.insert(k.clone(), v.clone());
        }
        let line = format!("{}\n", Value::Object(row));
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

// ------------------------------------------------------------------ time ----

/// Days since 1970-01-01 for a proleptic Gregorian civil date (H. Hinnant's algorithm).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * ((m + 9) % 12) + 2) / 5 + d - 1;
    era * 146_097 + yoe * 365 + yoe / 4 - yoe / 100 + doy - 719_468
}

/// `YYYY-MM-DD` as milliseconds at 00:00Z: month 1-12, day 1-31 (an overflowing day rolls
/// into the next month, the reading the retention prune has always used); anything else `None`.
pub fn day_ms(day: &str) -> Option<f64> {
    let b = day.as_bytes();
    let shaped = b.len() == 10
        && b[4] == b'-'
        && b[7] == b'-'
        && b.iter()
            .enumerate()
            .all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit());
    if !shaped {
        return None;
    }
    let n = |r: std::ops::Range<usize>| day[r].parse::<i64>().ok();
    let (y, m, d) = (n(0..4)?, n(5..7)?, n(8..10)?);
    ((1..=12).contains(&m) && (1..=31).contains(&d))
        .then(|| (days_from_civil(y, m, d) * 86_400_000) as f64)
}

/// Milliseconds since the epoch, now.
pub fn now_ms() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_millis() as f64)
}

/// The UTC ISO-8601 timestamp `YYYY-MM-DDTHH:MM:SS.sssZ` of an epoch-milliseconds instant.
pub fn iso_from_ms(ms: f64) -> String {
    let ms = ms.floor() as i64;
    let (z, rem) = (
        ms.div_euclid(86_400_000) + 719_468,
        ms.rem_euclid(86_400_000),
    );
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{:03}Z",
        rem / 3_600_000,
        rem / 60_000 % 60,
        rem / 1000 % 60,
        rem % 1000
    )
}

pub fn iso_now() -> String {
    iso_from_ms(now_ms())
}

/// The four credential headers become `<redacted>`, order kept.
/// A repeated header logs its values joined with `, `.
pub fn redact_headers(headers: &HeaderMap) -> Map<String, Value> {
    let mut out = Map::new();
    for k in headers.keys() {
        let name = k.as_str();
        let secret = matches!(
            name,
            "authorization" | "x-api-key" | "cookie" | "proxy-authorization"
        );
        let v = if secret {
            "<redacted>".to_string()
        } else {
            headers
                .get_all(k)
                .iter()
                .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned())
                .collect::<Vec<_>>()
                .join(", ")
        };
        out.insert(name.to_string(), Value::from(v));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn day_parse_and_iso_format() {
        // 2021-02-30 reads as 2021-03-02 = 1614643200000 ms (the day rolls over)
        assert_eq!(day_ms("2021-02-30"), Some(1_614_643_200_000.0));
        assert_eq!(day_ms("2020-02-29"), Some(1_582_934_400_000.0));
        for bad in [
            "2021-13-01",
            "2021-01-00",
            "2021-1-01",
            "2021-01-01x",
            "+021-01-01",
        ] {
            assert_eq!(day_ms(bad), None, "{bad}");
        }
        let t = day_ms("2026-10-13").unwrap() + 86_399_999.0;
        assert_eq!(iso_from_ms(t), "2026-10-13T23:59:59.999Z");
        assert_eq!(iso_from_ms(0.0), "1970-01-01T00:00:00.000Z");
    }
}
