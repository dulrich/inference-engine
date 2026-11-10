//! `gateway.config.json` + env. The file is typed (`serde`, unknown keys rejected, one
//! spelling per key) and every malformed provider is a startup error; env values come from
//! `crate::env` (native parses; an invalid server setting refuses to start).

use serde::Deserialize;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::sync::Arc;

pub use crate::env::Env;
use crate::env::{self as envp, trimmed as read_str};

const DEFAULT_BODY_LIMIT: usize = 50 * 1024 * 1024;
/// Connect bound per attempt; with the one connect-only retry a blackholed upstream fails in
/// about 2 × 3 s + 150 ms.
pub const DEFAULT_CONNECT_TIMEOUT_MS: u64 = 3000;
/// Effort levels an `effortKwargs` row may name (the levels `derive_effort_level` emits).
const EFFORT_LEVELS: [&str; 6] = ["low", "medium", "high", "xhigh", "max", "disabled"];

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
pub enum Kind {
    #[serde(rename = "anthropic")]
    Anthropic,
    #[serde(rename = "openai_chat_completions")]
    OpenAiChat,
    #[serde(rename = "systemone")]
    Systemone,
}

impl Kind {
    /// The provider-family token used in envelopes and routing headers.
    pub fn token(&self) -> &'static str {
        match self {
            Kind::Anthropic => "anthropic",
            Kind::OpenAiChat => "openai",
            Kind::Systemone => "systemone",
        }
    }
}

/// Who supplies the upstream credential (`credential` in the config; absent = provider key).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize)]
pub enum Credential {
    /// The provider's own `apikey`/`apiKeyEnv`; unresolved at runtime → per-request 503.
    #[default]
    #[serde(skip)]
    ProviderKey,
    /// The caller's `x-api-key` or OAuth bearer is relayed (router/AGENTS.md 0001), and the
    /// caller's `user-agent` is forwarded on every route. No provider key may be configured.
    #[serde(rename = "caller-oauth-passthrough")]
    CallerOauthPassthrough,
    /// Router chaining (multi-node): the upstream is another inference-router. Only
    /// `x-gateway-key: <apiKeyEnv value>` is sent; the caller's `authorization`, `x-api-key` and
    /// `x-gateway-key` never are, and the model id is forwarded verbatim (prefix included).
    /// Needs `type:"anthropic"` + `apiKeyEnv`; forms its own default-route class.
    #[serde(rename = "upstream-gateway")]
    UpstreamGateway,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
enum LocalLeg {
    #[serde(rename = "native-messages")]
    NativeMessages,
}

/// model → effort level → kwargs merged into `chat_template_kwargs`.
pub type EffortKwargs = HashMap<String, HashMap<String, Map<String, Value>>>;
/// model → positive output-token cap.
pub type MaxOutputTokens = HashMap<String, u64>;

/// A provider. The static config supplies everything but the model fields; `models`,
/// `effort_kwargs` and `max_output_tokens` are empty on `Settings::providers` and filled from
/// the live catalog (`crate::catalog`), which is the only routing source.
#[derive(Clone, Debug)]
pub struct Provider {
    pub name: String,
    pub kind: Kind,
    pub credential: Credential,
    /// `apikey`, else `apiKeyEnv` resolved at load. `None` = uncredentialed.
    pub apikey: Option<String>,
    pub baseurl: Option<String>,
    pub models: Vec<String>,
    pub native_messages: bool,
    /// The static config names a credential (`apikey`, `apiKeyEnv` or caller passthrough),
    /// whether or not it resolves at runtime. The catalog refuses an empty model list here.
    pub credentialed: bool,
    pub effort_kwargs: EffortKwargs,
    pub max_output_tokens: MaxOutputTokens,
}

#[derive(Debug)]
pub struct Settings {
    pub host: String,
    pub port: u16,
    /// Static providers in config order; model fields empty (see `Provider`).
    pub providers: Vec<Arc<Provider>>,
    /// `AUTH_STATIC_API_KEYS`, never empty: client auth is always on (`auth.rs`).
    pub gateway_keys: Vec<String>,
    pub body_limit: usize,
    /// 0 = no timeout.
    pub upstream_timeout_ms: u64,
    /// `UPSTREAM_CONNECT_TIMEOUT_MS` (>= 1; unset = `DEFAULT_CONNECT_TIMEOUT_MS`).
    pub upstream_connect_timeout_ms: u64,
    pub anthropic_version: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FileConfig {
    host: Option<String>,
    port: Option<u16>,
    providers: Vec<Value>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FileProvider {
    name: String,
    #[serde(rename = "type")]
    kind: Kind,
    #[serde(default)]
    credential: Credential,
    apikey: Option<String>,
    #[serde(rename = "apiKeyEnv")]
    api_key_env: Option<String>,
    baseurl: Option<String>,
    #[serde(rename = "localLeg")]
    local_leg: Option<LocalLeg>,
}

fn list(raw: Option<String>) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for item in raw.unwrap_or_default().split(',') {
        let item = item.trim();
        if !item.is_empty() && !out.iter().any(|o| o == item) {
            out.push(item.to_string());
        }
    }
    out
}

fn parse_provider(item: &Value, env: Env) -> Result<Provider, String> {
    let f = FileProvider::deserialize(item).map_err(|e| e.to_string())?;
    let apikey = read_str(f.apikey);
    let key_env = read_str(f.api_key_env);
    // There is no managed Anthropic key concept (router/AGENTS.md "Never set
    // `ANTHROPIC_API_KEY`"), so naming it is refused rather than silently ignored.
    if key_env.as_deref() == Some("ANTHROPIC_API_KEY") {
        return Err("apiKeyEnv \"ANTHROPIC_API_KEY\" is never read; remove it".into());
    }
    if f.credential == Credential::CallerOauthPassthrough {
        if apikey.is_some() || key_env.is_some() {
            return Err(
                "credential \"caller-oauth-passthrough\" contradicts a configured apikey/apiKeyEnv"
                    .into(),
            );
        }
        if f.kind != Kind::Anthropic {
            return Err("credential \"caller-oauth-passthrough\" needs type \"anthropic\"".into());
        }
    }
    if f.credential == Credential::UpstreamGateway {
        if apikey.is_some() || key_env.is_none() {
            return Err(
                "credential \"upstream-gateway\" needs apiKeyEnv (and no literal apikey)".into(),
            );
        }
        if f.kind != Kind::Anthropic {
            return Err("credential \"upstream-gateway\" needs type \"anthropic\"".into());
        }
    }
    if f.local_leg.is_some() && f.kind != Kind::OpenAiChat {
        return Err("localLeg needs type \"openai_chat_completions\"".into());
    }
    let credentialed =
        apikey.is_some() || key_env.is_some() || f.credential == Credential::CallerOauthPassthrough;
    // An `apiKeyEnv` unset at runtime leaves the leg uncredentialed (per-request 503), never a
    // startup error: optional legs must not stop the gateway.
    let apikey = apikey.or_else(|| key_env.and_then(|e| read_str(env(&e))));
    Ok(Provider {
        name: f.name.trim().to_string(),
        kind: f.kind,
        credential: f.credential,
        apikey,
        baseurl: read_str(f.baseurl).map(|b| b.trim_end_matches('/').to_string()),
        models: Vec::new(),
        native_messages: f.local_leg == Some(LocalLeg::NativeMessages),
        credentialed,
        effort_kwargs: EffortKwargs::new(),
        max_output_tokens: MaxOutputTokens::new(),
    })
}

/// Every level an `effortKwargs` row names is in the closed `EFFORT_LEVELS` set.
pub(crate) fn check_effort_levels(
    model: &str,
    levels: &HashMap<String, Map<String, Value>>,
) -> Result<(), String> {
    match levels.keys().find(|l| !EFFORT_LEVELS.contains(&l.as_str())) {
        Some(l) => Err(format!(
            "effortKwargs[{model:?}] level {l:?} is not one of {EFFORT_LEVELS:?}"
        )),
        None => Ok(()),
    }
}

type Parsed = (FileConfig, Vec<Arc<Provider>>);

fn parse_file(raw: &Value, env: Env) -> Result<Parsed, String> {
    let file = FileConfig::deserialize(raw).map_err(|e| format!("gateway config: {e}"))?;
    let mut providers = Vec::new();
    for (i, p) in file.providers.iter().enumerate() {
        let label = p.get("name").and_then(Value::as_str).unwrap_or("?");
        let p = parse_provider(p, env)
            .map_err(|e| format!("gateway config providers[{i}] ({label}): {e}"))?;
        providers.push(Arc::new(p));
    }
    Ok((file, providers))
}

/// The static providers only — no server env (`AUTH_STATIC_API_KEYS`, `PORT`, …) is required.
/// Used by `inference-router check-catalog`.
pub fn load_static_providers(env: Env) -> Result<Vec<Arc<Provider>>, String> {
    let raw = read_file(env)?;
    parse_file(&raw, env).map(|(_, p)| p)
}

pub fn from_json(raw: &Value, env: Env) -> Result<Settings, String> {
    let (file, providers) = parse_file(raw, env)?;
    let host = read_str(env("HOST"))
        .or_else(|| read_str(file.host))
        .unwrap_or_else(|| "0.0.0.0".into());
    let port = envp::integer::<u16>(env, "PORT")?
        .or(file.port)
        .unwrap_or(3000);
    let body_limit = match envp::integer::<usize>(env, "GATEWAY_BODY_LIMIT_BYTES")? {
        Some(0) => {
            return Err("GATEWAY_BODY_LIMIT_BYTES=0 is not a valid body limit (>= 1)".into())
        }
        Some(n) => n,
        None => DEFAULT_BODY_LIMIT,
    };
    let upstream_timeout_ms = envp::integer::<u64>(env, "UPSTREAM_TIMEOUT_MS")?.unwrap_or(0);
    let upstream_connect_timeout_ms =
        match envp::integer::<u64>(env, "UPSTREAM_CONNECT_TIMEOUT_MS")? {
            Some(0) => {
                return Err(
                    "UPSTREAM_CONNECT_TIMEOUT_MS=0 is not a valid connect timeout (>= 1)".into(),
                )
            }
            Some(n) => n,
            None => DEFAULT_CONNECT_TIMEOUT_MS,
        };
    let gateway_keys = list(env("AUTH_STATIC_API_KEYS"));
    if gateway_keys.is_empty() {
        return Err(
            "AUTH_STATIC_API_KEYS is empty or unset; client auth is always on, configure at least one gateway key"
                .into(),
        );
    }
    Ok(Settings {
        host,
        port,
        providers,
        gateway_keys,
        body_limit,
        upstream_timeout_ms,
        upstream_connect_timeout_ms,
        anthropic_version: read_str(env("ANTHROPIC_VERSION"))
            .unwrap_or_else(|| "2023-06-01".into()),
    })
}

/// `GATEWAY_CONFIG_PATH`, else `./gateway.config.json`.
pub fn load(env: Env) -> Result<Settings, String> {
    from_json(&read_file(env)?, env)
}

fn read_file(env: Env) -> Result<Value, String> {
    let path = read_str(env("GATEWAY_CONFIG_PATH")).unwrap_or_else(|| "gateway.config.json".into());
    let text = std::fs::read_to_string(&path).map_err(|e| format!("read {path}: {e}"))?;
    serde_json::from_str(&text).map_err(|e| format!("parse {path}: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |k| m.get(k).cloned()
    }

    const KEYS: (&str, &str) = ("AUTH_STATIC_API_KEYS", "gk");

    fn err_of(raw: Value, env: &[(&str, &str)]) -> String {
        let mut env = env.to_vec();
        env.push(KEYS);
        from_json(&raw, &env_of(&env)).unwrap_err()
    }

    fn one(p: Value) -> Value {
        json!({"host": "0.0.0.0", "port": 4813, "providers": [p]})
    }

    #[test]
    fn env_beats_config_and_api_key_env_resolves() {
        let raw = json!({"host":"0.0.0.0","port":4813,"providers":[
            {"name":"or","type":"openai_chat_completions","apiKeyEnv":"OR_KEY","baseurl":"https://x/v1/"},
            {"name":"anthropic","type":"anthropic","credential":"caller-oauth-passthrough","baseurl":"https://api.anthropic.com"}]});
        let env = env_of(&[
            ("HOST", "127.0.0.1"),
            ("PORT", "4999"),
            ("OR_KEY", "k1"),
            ("AUTH_STATIC_API_KEYS", "a, b, a"),
        ]);
        let s = from_json(&raw, &env).unwrap();
        assert_eq!((s.host.as_str(), s.port), ("127.0.0.1", 4999));
        assert_eq!(s.providers.len(), 2);
        assert_eq!(s.providers[0].apikey.as_deref(), Some("k1"));
        assert_eq!(s.providers[0].baseurl.as_deref(), Some("https://x/v1"));
        assert_eq!(s.providers[1].apikey, None);
        assert_eq!(s.gateway_keys, vec!["a", "b"]);
        assert_eq!((s.body_limit, s.upstream_timeout_ms), (50 * 1024 * 1024, 0));
        let s = from_json(&raw, &env_of(&[KEYS])).unwrap();
        assert_eq!((s.host.as_str(), s.port), ("0.0.0.0", 4813));
        assert_eq!(s.providers[0].apikey, None);
    }

    #[test]
    fn fixture_config_loads_under_the_strict_loader() {
        let text = include_str!("../../test/fixtures/gateway.config.fixture.json");
        let raw: Value = serde_json::from_str(text).unwrap();
        let s = from_json(&raw, &env_of(&[KEYS])).unwrap();
        let local = s
            .providers
            .iter()
            .find(|p| p.name == "claude-local")
            .unwrap();
        assert!(local.native_messages && local.apikey.is_some() && local.credentialed);
        // Model fields come only from the catalog.
        assert!(local.models.is_empty() && local.effort_kwargs.is_empty());
        assert!(s
            .providers
            .iter()
            .filter(|p| p.name != "claude-local")
            .all(|p| !p.native_messages));
        let passthrough: Vec<_> = s
            .providers
            .iter()
            .filter(|p| p.credential == Credential::CallerOauthPassthrough)
            .map(|p| p.name.as_str())
            .collect();
        assert_eq!(passthrough, ["anthropic"]);
    }

    #[test]
    fn rejects_an_unknown_key_and_an_alias_spelling() {
        let e = err_of(json!({"providers": [], "bodyLimit": 1}), &[]);
        assert!(e.contains("unknown field `bodyLimit`"), "{e}");
        let e = err_of(
            one(json!({"name": "x", "type": "anthropic", "baseUrl": "https://a"})),
            &[],
        );
        assert!(
            e.starts_with("gateway config providers[0] (x): unknown field `baseUrl`"),
            "{e}"
        );
        let e = err_of(json!({"Providers": []}), &[]);
        assert!(e.contains("unknown field `Providers`"), "{e}");
    }

    #[test]
    fn legacy_per_model_keys_are_a_startup_error() {
        for (k, v) in [
            ("models", json!(["m"])),
            ("effortKwargs", json!({"m": {"low": {}}})),
            ("maxOutputTokens", json!({"m": 1})),
            ("consumers", json!(["router"])),
        ] {
            let mut p = json!({"name": "x", "type": "anthropic", "apikey": "k"});
            p[k] = v;
            let e = err_of(one(p), &[]);
            assert!(e.contains(&format!("unknown field `{k}`")), "{e}");
        }
    }

    #[test]
    fn rejects_a_bad_type() {
        let e = err_of(one(json!({"name": "x", "type": "openai"})), &[]);
        assert!(
            e.starts_with("gateway config providers[0] (x): unknown variant `openai`"),
            "{e}"
        );
    }

    #[test]
    fn rejects_a_bad_port_and_body_limit() {
        let raw = json!({"providers": []});
        for bad in ["70000", "4813.9", "1e3", "inf", "-1"] {
            let e = err_of(raw.clone(), &[("PORT", bad)]);
            assert!(e.starts_with("PORT="), "{e}");
        }
        let e = err_of(raw.clone(), &[("GATEWAY_BODY_LIMIT_BYTES", "0")]);
        assert!(e.starts_with("GATEWAY_BODY_LIMIT_BYTES=0"), "{e}");
        // The removed alias is not read.
        let s = from_json(&raw, &env_of(&[KEYS, ("BODY_LIMIT_BYTES", "0")])).unwrap();
        assert_eq!(s.body_limit, DEFAULT_BODY_LIMIT);
        let s = from_json(&raw, &env_of(&[KEYS, ("PORT", "")])).unwrap();
        assert_eq!(s.port, 3000);
    }

    #[test]
    fn rejects_a_contradictory_credential_policy_and_anthropic_api_key() {
        let e = err_of(
            one(
                json!({"name": "a", "type": "anthropic", "credential": "caller-oauth-passthrough", "apiKeyEnv": "K"}),
            ),
            &[],
        );
        assert!(
            e.contains("contradicts a configured apikey/apiKeyEnv"),
            "{e}"
        );
        let e = err_of(
            one(json!({"name": "a", "type": "anthropic", "apiKeyEnv": "ANTHROPIC_API_KEY"})),
            &[],
        );
        assert!(e.contains("ANTHROPIC_API_KEY"), "{e}");
        // upstream-gateway: apiKeyEnv only, type anthropic only.
        for (p, want) in [
            (
                json!({"name": "c", "type": "anthropic", "credential": "upstream-gateway"}),
                "needs apiKeyEnv",
            ),
            (
                json!({"name": "c", "type": "anthropic", "credential": "upstream-gateway", "apikey": "k", "apiKeyEnv": "K"}),
                "needs apiKeyEnv",
            ),
            (
                json!({"name": "c", "type": "openai_chat_completions", "credential": "upstream-gateway", "apiKeyEnv": "K"}),
                "needs type \"anthropic\"",
            ),
        ] {
            let e = err_of(one(p), &[]);
            assert!(e.contains(want), "{e}");
        }
        let e = err_of(
            json!({"providers": []}),
            &[("UPSTREAM_CONNECT_TIMEOUT_MS", "0")],
        );
        assert!(e.starts_with("UPSTREAM_CONNECT_TIMEOUT_MS=0"), "{e}");
    }

    #[test]
    fn refuses_to_start_without_gateway_keys() {
        let raw = json!({"providers": []});
        for env in [
            vec![],
            vec![("AUTH_STATIC_API_KEYS", " , ")],
            vec![("AUTH_STATIC_API_KEY", "gk"), ("AUTH_ENABLED", "false")],
        ] {
            let e = from_json(&raw, &env_of(&env)).unwrap_err();
            assert!(
                e.starts_with("AUTH_STATIC_API_KEYS is empty or unset"),
                "{e}"
            );
        }
    }
}
