//! The process's pinned inputs and its environment reading.
//!
//! Two kinds of value live here and the distinction is the point: the PINNED ones (preamble,
//! thread config, the in-band item-type allowlist) are contract and deliberately NOT
//! env-overridable, and `read_config` reads everything that is. Integer knobs are strict:
//! surrounding whitespace is trimmed, unset/empty takes the default, and anything that is not
//! plain ASCII digits in range is a startup error naming the variable.

use crate::logging::Logger;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::fmt;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};

pub const VERSION: &str = "0.1.0";
pub const BODY_LIMIT: usize = 32 * 1024 * 1024;

/// The execution boundary: any other item type is out of band and trips it.
/// `contextCompaction` is in band because it is codex's own context management (the
/// `model_auto_compact_token_limit` lever in `thread-config.json`), not execution.
pub const INBAND_ITEM_TYPES: &[&str] = &[
    "agentMessage",
    "reasoning",
    "dynamicToolCall",
    "userMessage",
    "contextCompaction",
];

/// The pinned files `preamble.txt` and `thread-config.json`, read at startup from the sidecar
/// source dir (never copied, never embedded).
#[derive(Debug, Clone)]
pub struct Pinned {
    pub dir: PathBuf,
    /// `preamble.txt` with trailing newlines stripped (`.replace(/\n+$/, "")`).
    pub preamble: String,
    pub thread_config: Value,
}

fn has_pinned(dir: &Path) -> bool {
    dir.join("preamble.txt").is_file() && dir.join("thread-config.json").is_file()
}

/// Where the pinned files live. Not env-overridable (they are contract): the nearest ancestor
/// of the running executable holding both files (`sidecar/rs/target/release/codex-sidecar` ->
/// `sidecar/`), else the source tree this binary was built from (`CARGO_MANIFEST_DIR/..`).
pub fn pinned_dir() -> PathBuf {
    if let Ok(exe) = std::env::current_exe() {
        let exe = exe.canonicalize().unwrap_or(exe);
        for a in exe.ancestors().skip(1) {
            if has_pinned(a) {
                return a.to_path_buf();
            }
        }
    }
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap_or(Path::new("."))
        .to_path_buf()
}

impl Pinned {
    pub fn load_from(dir: &Path) -> Result<Self, String> {
        let p = dir.join("preamble.txt");
        let preamble = std::fs::read_to_string(&p).map_err(|e| format!("{}: {e}", p.display()))?;
        let t = dir.join("thread-config.json");
        let raw = std::fs::read_to_string(&t).map_err(|e| format!("{}: {e}", t.display()))?;
        let thread_config =
            serde_json::from_str(&raw).map_err(|e| format!("{}: {e}", t.display()))?;
        Ok(Self {
            dir: dir.to_path_buf(),
            preamble: preamble.trim_end_matches('\n').to_string(),
            thread_config,
        })
    }

    pub fn load() -> Result<Self, String> {
        Self::load_from(&pinned_dir())
    }
}

/// The environment, read once at startup.
#[derive(Clone)]
pub struct Config {
    /// Required by the server; never logged (redacted from `Debug`).
    pub key: String,
    pub port: u16,
    /// `""` = skip the extra bind (`SIDECAR_BIND_EXTRA=""`); unset = the docker0 gateway.
    pub bind_extra: String,
    pub personas: Vec<String>,
    pub cwd_fallback: String,
    pub state_dir: String,
    pub log_dir: String,
    pub log_keep_days: u32,
    /// SQLite-era name; the outbox writer only uses its directory. Empty = writer off.
    pub outbox_path: String,
    pub context_window_tokens: u64,
    // ---- test seams (README "Test seams"; they never weaken an invariant) ----
    pub app_server_cmd: String,
    pub ping_ms: u64,
    pub batch_ms: u64,
    pub turn_timeout_ms: u64,
    pub backoff_ms: u64,
    pub bind_retry_ms: u64,
    pub bind_retry_budget_ms: u64,
    pub env: HashMap<String, String>,
}

impl fmt::Debug for Config {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Config")
            .field(
                "key",
                &if self.key.is_empty() {
                    ""
                } else {
                    "<redacted>"
                },
            )
            .field("port", &self.port)
            .field("bind_extra", &self.bind_extra)
            .field("personas", &self.personas)
            .field("cwd_fallback", &self.cwd_fallback)
            .field("state_dir", &self.state_dir)
            .field("log_dir", &self.log_dir)
            .field("log_keep_days", &self.log_keep_days)
            .field("outbox_path", &self.outbox_path)
            .field("context_window_tokens", &self.context_window_tokens)
            .field("app_server_cmd", &self.app_server_cmd)
            .finish_non_exhaustive()
    }
}

/// The process environment as `process.env` sees it (non-UTF-8 lossily decoded).
pub fn process_env() -> HashMap<String, String> {
    std::env::vars_os()
        .map(|(k, v)| {
            (
                k.to_string_lossy().into_owned(),
                v.to_string_lossy().into_owned(),
            )
        })
        .collect()
}

fn join(base: &str, parts: &[&str]) -> String {
    let mut p = PathBuf::from(base);
    for x in parts {
        p.push(x);
    }
    p.to_string_lossy().into_owned()
}

/// Strict unsigned integer: trimmed, ASCII digits only, in range for `T`; unset/empty = `d`.
fn int<T: std::str::FromStr>(env: &HashMap<String, String>, k: &str, d: T) -> Result<T, String> {
    let Some(raw) = env.get(k) else {
        return Ok(d);
    };
    let v = raw.trim();
    if v.is_empty() {
        return Ok(d);
    }
    if !v.bytes().all(|b| b.is_ascii_digit()) {
        return Err(format!("{k}: invalid integer {raw:?}"));
    }
    v.parse::<T>()
        .map_err(|_| format!("{k}: integer out of range {raw:?}"))
}

/// Reads the environment; an invalid integer knob is an error naming the variable.
pub fn read_config(env: &HashMap<String, String>) -> Result<Config, String> {
    let get = |k: &str| env.get(k).map(String::as_str);
    // Unset and empty both take the default.
    let or = |k: &str, d: String| get(k).filter(|s| !s.is_empty()).map_or(d, str::to_string);
    #[allow(deprecated)]
    let os_home = || {
        std::env::home_dir()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default()
    };
    let home = get("HOME")
        .filter(|s| !s.is_empty())
        .map_or_else(os_home, str::to_string);
    let state_dir = or(
        "SIDECAR_STATE_DIR",
        join(&home, &[".local", "state", "codex-sidecar"]),
    );
    Ok(Config {
        key: get("SIDECAR_KEY").unwrap_or("").to_string(),
        port: int(env, "SIDECAR_PORT", 4877)?,
        bind_extra: get("SIDECAR_BIND_EXTRA")
            .unwrap_or("172.17.0.1")
            .to_string(),
        personas: get("SIDECAR_PERSONAS")
            .unwrap_or("codex-a,codex-b")
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect(),
        cwd_fallback: or("SIDECAR_CWD", join(&state_dir, &["empty-cwd"])),
        log_dir: or("SIDECAR_LOG_DIR", join(&state_dir, &["logs"])),
        log_keep_days: int(env, "SIDECAR_LOG_KEEP_DAYS", 14)?,
        outbox_path: get("SIDECAR_OUTBOX_PATH").unwrap_or("").to_string(),
        context_window_tokens: int(env, "SIDECAR_CONTEXT_WINDOW_TOKENS", 258_400)?,
        app_server_cmd: get("SIDECAR_APP_SERVER_CMD").unwrap_or("").to_string(),
        ping_ms: int(env, "SIDECAR_PING_MS", 15_000)?,
        batch_ms: int(env, "SIDECAR_BATCH_MS", 200)?,
        turn_timeout_ms: int(env, "SIDECAR_TURN_TIMEOUT_MS", 300_000)?,
        backoff_ms: int(env, "SIDECAR_BACKOFF_MS", 1000)?,
        bind_retry_ms: int(env, "SIDECAR_BIND_RETRY_MS", 500)?,
        bind_retry_budget_ms: int(env, "SIDECAR_BIND_RETRY_BUDGET_MS", 60_000)?,
        state_dir,
        env: env.clone(),
    })
}

/// The thread's cwd: the client's own working directory when it names a
/// real one, else the configured fallback, else an empty directory created on demand so no
/// stray AGENTS.md is loaded from somebody else's tree.
pub fn resolve_cwd(
    config: &Config,
    logger: &mut Logger,
    raw_cwd: Option<&str>,
) -> std::io::Result<String> {
    for c in [raw_cwd, Some(config.cwd_fallback.as_str())]
        .into_iter()
        .flatten()
    {
        if !c.is_empty() && std::fs::metadata(c).is_ok_and(|m| m.is_dir()) {
            return Ok(c.to_string());
        }
    }
    let empty = join(&config.state_dir, &["empty-cwd"]);
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&empty)?;
    if let Some(r) = raw_cwd.filter(|r| !r.is_empty()) {
        let mut m = Map::new();
        m.insert("requested".into(), Value::from(r));
        m.insert("used".into(), Value::from(empty.as_str()));
        logger.write("cwd-fallback", &m);
    }
    Ok(empty)
}
