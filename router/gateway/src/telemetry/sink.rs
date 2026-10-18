//! The 0006 jsonl sink: whole-line buffered appends on a dedicated writer thread, UTC-day +
//! size rotation to `router-YYYY-MM-DD.N.jsonl`, `keepFiles` retention, and fail-open (a sink
//! error disables the sink with one warning; it never fails or delays a request).

use crate::env::{self, Env};
use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Flush threshold for the pending whole-line buffer, and the per-line cap (0006).
pub const LINE_CAP_BYTES: usize = 64 * 1024;
const DEFAULT_DIR: &str = "/var/log/inference-router";
const DEFAULT_MAX_BYTES: u64 = 16 * 1024 * 1024;
const DEFAULT_KEEP_FILES: usize = 14;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SinkOptions {
    pub dir: PathBuf,
    pub max_bytes: u64,
    pub keep_files: usize,
}

pub type Clock = Arc<dyn Fn() -> SystemTime + Send + Sync>;

pub fn system_clock() -> Clock {
    Arc::new(SystemTime::now)
}

/// `ROUTER_TELEMETRY_{ENABLED,DIR,MAX_BYTES,KEEP_FILES}` with the 0006 defaults. Invalid values
/// warn once and take the default (the sink is fail-open).
pub fn options_from_env(env: Env) -> (bool, SinkOptions) {
    let dir = env::string(env, "ROUTER_TELEMETRY_DIR").unwrap_or_else(|| DEFAULT_DIR.to_string());
    (
        env::flag_or_warn(env, "ROUTER_TELEMETRY_ENABLED", true),
        SinkOptions {
            dir: PathBuf::from(dir),
            max_bytes: env::positive_or_warn(env, "ROUTER_TELEMETRY_MAX_BYTES", DEFAULT_MAX_BYTES),
            keep_files: env::positive_or_warn(
                env,
                "ROUTER_TELEMETRY_KEEP_FILES",
                DEFAULT_KEEP_FILES,
            ),
        },
    )
}

/// Days since the epoch → (year, month, day), proleptic Gregorian (Hinnant's civil_from_days).
fn civil(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

fn split(t: SystemTime) -> (i64, u64) {
    let ms = t
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_millis() as u64;
    ((ms / 86_400_000) as i64, ms % 86_400_000)
}

/// `YYYY-MM-DD` (UTC).
pub fn utc_day(t: SystemTime) -> String {
    let (y, m, d) = civil(split(t).0);
    format!("{y:04}-{m:02}-{d:02}")
}

/// `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC), the JS `toISOString` shape.
pub fn iso8601(t: SystemTime) -> String {
    let (days, ms) = split(t);
    let (y, m, d) = civil(days);
    let s = ms / 1000;
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{:03}Z",
        s / 3600,
        (s / 60) % 60,
        s % 60,
        ms % 1000
    )
}

enum Msg {
    Line(String),
    Flush(Sender<()>),
}

/// Cheap, non-blocking handle: `write` serializes and enqueues; the thread does all I/O.
pub struct Sink {
    tx: Sender<Msg>,
    disabled: Arc<AtomicBool>,
}

impl Sink {
    pub fn new(options: SinkOptions, clock: Clock) -> Arc<Sink> {
        let (tx, rx) = mpsc::channel();
        let disabled = Arc::new(AtomicBool::new(false));
        let mut w = Writer {
            max_bytes: options.max_bytes.max(1024),
            keep_files: options.keep_files.max(1),
            dir: options.dir,
            clock,
            disabled: disabled.clone(),
            day: None,
            index: 0,
            current: 0,
            prune: false,
        };
        let spawned = std::thread::Builder::new()
            .name("router-telemetry".into())
            .spawn(move || w.run(rx));
        if spawned.is_err() {
            disabled.store(true, Ordering::Relaxed);
        }
        Arc::new(Sink { tx, disabled })
    }

    pub fn is_disabled(&self) -> bool {
        self.disabled.load(Ordering::Relaxed)
    }

    /// Queue one record. Never blocks on I/O, never panics; an oversized line is dropped.
    pub fn write(&self, record: &Value) {
        if self.is_disabled() {
            return;
        }
        let mut line = record.to_string();
        line.push('\n');
        if line.len() > LINE_CAP_BYTES {
            return;
        }
        let _ = self.tx.send(Msg::Line(line));
    }

    /// Block until every line queued before this call has been attempted (tests; nothing calls it at shutdown).
    pub fn flush(&self) {
        let (ack, done) = mpsc::channel();
        if self.tx.send(Msg::Flush(ack)).is_ok() {
            let _ = done.recv_timeout(Duration::from_secs(5));
        }
    }
}

struct Writer {
    dir: PathBuf,
    max_bytes: u64,
    keep_files: usize,
    clock: Clock,
    disabled: Arc<AtomicBool>,
    day: Option<String>,
    index: u64,
    current: u64,
    prune: bool,
}

fn parse_name(name: &str) -> Option<(&str, u64)> {
    let rest = name.strip_prefix("router-")?.strip_suffix(".jsonl")?;
    let (day, idx) = rest.split_once('.')?;
    let b = day.as_bytes();
    let day_ok = b.len() == 10
        && b.iter().enumerate().all(|(i, c)| {
            if i == 4 || i == 7 {
                *c == b'-'
            } else {
                c.is_ascii_digit()
            }
        });
    let idx_ok = !idx.is_empty() && idx.bytes().all(|c| c.is_ascii_digit());
    if !(day_ok && idx_ok) {
        return None;
    }
    Some((day, idx.parse().ok()?))
}

impl Writer {
    fn run(&mut self, rx: Receiver<Msg>) {
        let mut buf = String::new();
        while let Ok(first) = rx.recv() {
            let mut acks = Vec::new();
            let mut next = Some(first);
            while let Some(msg) = next.take() {
                match msg {
                    Msg::Line(l) => buf.push_str(&l),
                    Msg::Flush(a) => acks.push(a),
                }
                if buf.len() < LINE_CAP_BYTES && acks.is_empty() {
                    next = rx.try_recv().ok();
                }
            }
            if !buf.is_empty() {
                self.append(&buf);
                buf.clear();
            }
            for a in acks {
                let _ = a.send(());
            }
        }
    }

    fn disable(&mut self, why: &str) {
        if !self.disabled.swap(true, Ordering::Relaxed) {
            eprintln!(
                "router request telemetry disabled: sink directory {} is missing or not writable ({why})",
                self.dir.display()
            );
        }
    }

    fn append(&mut self, payload: &str) {
        if self.disabled.load(Ordering::Relaxed) {
            return;
        }
        if self.day.is_none() {
            if let Err(e) = fs::create_dir_all(&self.dir) {
                return self.disable(&e.to_string());
            }
        }
        let target = self.target(payload.len() as u64);
        let res = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&target)
            .and_then(|mut f| f.write_all(payload.as_bytes()));
        match res {
            Ok(()) => {
                self.current += payload.len() as u64;
                if std::mem::take(&mut self.prune) {
                    self.prune_files();
                }
            }
            Err(e) => self.disable(&e.to_string()),
        }
    }

    fn file(&self, day: &str, index: u64) -> PathBuf {
        self.dir.join(format!("router-{day}.{index}.jsonl"))
    }

    fn target(&mut self, incoming: u64) -> PathBuf {
        let day = utc_day((self.clock)());
        if self.day.as_deref() != Some(day.as_str()) {
            self.index = self
                .list()
                .iter()
                .filter_map(|n| parse_name(n).filter(|(d, _)| *d == day).map(|(_, i)| i))
                .max()
                .unwrap_or(0);
            self.current = size_of(&self.file(&day, self.index));
            self.day = Some(day.clone());
            self.prune = true;
        }
        if self.current > 0 && self.current + incoming > self.max_bytes {
            self.index += 1;
            self.current = 0;
            self.prune = true;
        }
        self.file(&day, self.index)
    }

    fn list(&self) -> Vec<String> {
        let mut v: Vec<(String, u64, String)> = fs::read_dir(&self.dir)
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| e.file_name().into_string().ok())
            .filter_map(|n| parse_name(&n).map(|(d, i)| (d.to_string(), i, n.clone())))
            .collect();
        // Oldest first by (UTC day, numeric index).
        v.sort();
        v.into_iter().map(|(_, _, n)| n).collect()
    }

    fn prune_files(&self) {
        let files = self.list();
        if files.len() > self.keep_files {
            for n in &files[..files.len() - self.keep_files] {
                let _ = fs::remove_file(self.dir.join(n));
            }
        }
    }
}

fn size_of(p: &Path) -> u64 {
    fs::metadata(p).map(|m| m.len()).unwrap_or(0)
}
