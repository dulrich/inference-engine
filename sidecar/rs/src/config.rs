//! config.mjs — the process's pinned inputs and its environment reading.
//!
//! Two kinds of value live here and the distinction is the point: the PINNED ones (preamble,
//! thread config, the in-band item-type allowlist) are contract and deliberately NOT
//! env-overridable, and `read_config` reads everything that is — with `config.mjs`'s exact
//! lenient semantics (B1: parity, not hardening; `Number(v)`, non-finite -> default).

use crate::js;
use crate::logging::Logger;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::fmt;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};

pub const VERSION: &str = "0.1.0";
pub const BODY_LIMIT: usize = 32 * 1024 * 1024;

/// The execution boundary: any other item type is out of band and trips it.
pub const INBAND_ITEM_TYPES: &[&str] = &[
    "agentMessage",
    "reasoning",
    "dynamicToolCall",
    "userMessage",
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

/// `readConfig(env)`'s result. Numbers stay `f64` exactly as JS holds them.
#[derive(Clone)]
pub struct Config {
    /// Required by the server; never logged (redacted from `Debug`).
    pub key: String,
    pub port: f64,
    /// `""` = skip the extra bind (`SIDECAR_BIND_EXTRA=""`); unset = the docker0 gateway.
    pub bind_extra: String,
    pub personas: Vec<String>,
    pub cwd_fallback: String,
    pub state_dir: String,
    pub log_dir: String,
    pub log_keep_days: f64,
    /// SQLite-era name; the outbox writer only uses its directory. Empty = writer off.
    pub outbox_path: String,
    pub context_window_tokens: f64,
    // ---- test seams (README "Test seams"; they never weaken an invariant) ----
    pub app_server_cmd: String,
    pub ping_ms: f64,
    pub batch_ms: f64,
    pub turn_timeout_ms: f64,
    pub backoff_ms: f64,
    pub bind_retry_ms: f64,
    pub bind_retry_budget_ms: f64,
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

/// `readConfig(env)`.
pub fn read_config(env: &HashMap<String, String>) -> Config {
    let get = |k: &str| env.get(k).map(String::as_str);
    // `env.X || d`: unset and empty both take the default.
    let or = |k: &str, d: String| get(k).filter(|s| !s.is_empty()).map_or(d, str::to_string);
    // `num(v, d)`: `Number(v)`, falling back on non-finite (unset is `Number(undefined)` = NaN).
    let num = |k: &str, d: f64| {
        get(k)
            .map(js::js_number)
            .filter(|n| n.is_finite())
            .unwrap_or(d)
    };
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
    Config {
        key: get("SIDECAR_KEY").unwrap_or("").to_string(),
        port: num("SIDECAR_PORT", 4877.0),
        bind_extra: get("SIDECAR_BIND_EXTRA")
            .unwrap_or("172.17.0.1")
            .to_string(),
        personas: get("SIDECAR_PERSONAS")
            .unwrap_or("codex-a,codex-b")
            .split(',')
            .map(js::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect(),
        cwd_fallback: or("SIDECAR_CWD", join(&state_dir, &["empty-cwd"])),
        log_dir: or("SIDECAR_LOG_DIR", join(&state_dir, &["logs"])),
        log_keep_days: num("SIDECAR_LOG_KEEP_DAYS", 14.0),
        outbox_path: get("SIDECAR_OUTBOX_PATH").unwrap_or("").to_string(),
        context_window_tokens: num("SIDECAR_CONTEXT_WINDOW_TOKENS", 258_400.0),
        app_server_cmd: get("SIDECAR_APP_SERVER_CMD").unwrap_or("").to_string(),
        ping_ms: num("SIDECAR_PING_MS", 15_000.0),
        batch_ms: num("SIDECAR_BATCH_MS", 200.0),
        turn_timeout_ms: num("SIDECAR_TURN_TIMEOUT_MS", 300_000.0),
        backoff_ms: num("SIDECAR_BACKOFF_MS", 1000.0),
        bind_retry_ms: num("SIDECAR_BIND_RETRY_MS", 500.0),
        bind_retry_budget_ms: num("SIDECAR_BIND_RETRY_BUDGET_MS", 60_000.0),
        state_dir,
        env: env.clone(),
    }
}

/// `resolveCwd(config, logger, rawCwd)`: the client's own working directory when it names a
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
