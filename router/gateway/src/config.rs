//! `gateway.config.json` + env, with the Node engine's precedence (`src/config.ts:965-1043`,
//! `parseProvidersConfig` `:3603`, auth `:2335`/`:2510`). Unknown keys are ignored.

use serde_json::Value;
use std::sync::Arc;

/// The additive provider key that marks an `openai_chat_completions` provider as also serving
/// Anthropic-Messages ingress natively at `<baseurl>/messages` (the local leg).
pub const LOCAL_LEG_KEY: &str = "localLeg";
pub const LOCAL_LEG_NATIVE_MESSAGES: &str = "native-messages";
const DEFAULT_BODY_LIMIT: usize = 50 * 1024 * 1024;

#[derive(Clone, Debug, PartialEq)]
pub enum Kind {
    Anthropic,
    OpenAiChat,
    Systemone,
    Other(String),
}

impl Kind {
    /// Node's `providerFromProviderType` token (used in envelopes and routing headers).
    pub fn token(&self) -> &str {
        match self {
            Kind::Anthropic => "anthropic",
            Kind::OpenAiChat => "openai",
            Kind::Systemone => "systemone",
            Kind::Other(s) => s,
        }
    }
}

#[derive(Debug)]
pub struct Provider {
    pub name: String,
    pub kind: Kind,
    /// `apikey`, else `apiKeyEnv` resolved at load. `None` = uncredentialed.
    pub apikey: Option<String>,
    pub baseurl: Option<String>,
    pub models: Vec<String>,
    pub native_messages: bool,
    pub effort_kwargs: Value,
    pub max_output_tokens: Value,
}

#[derive(Clone, Debug)]
pub struct AuthConfig {
    pub enabled: bool,
    pub required: bool,
    pub keys: Vec<String>,
    pub header: String,
    pub bearer_only: bool,
}

#[derive(Debug)]
pub struct Settings {
    pub host: String,
    pub port: u16,
    pub providers: Vec<Arc<Provider>>,
    pub auth: AuthConfig,
    pub body_limit: usize,
    /// 0 = no timeout (Node default).
    pub upstream_timeout_ms: u64,
    pub anthropic_version: String,
}

pub type Env<'a> = &'a dyn Fn(&str) -> Option<String>;

fn read_str(v: Option<String>) -> Option<String> {
    v.map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

fn json_str(v: &Value, key: &str) -> Option<String> {
    read_str(v.get(key).and_then(Value::as_str).map(str::to_string))
}

fn json_num(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

fn env_bool(raw: Option<String>, fallback: bool) -> bool {
    match read_str(raw).map(|s| s.to_ascii_lowercase()).as_deref() {
        Some("true" | "1" | "yes" | "on") => true,
        Some("false" | "0" | "no" | "off") => false,
        _ => fallback,
    }
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

fn parse_kind(token: &str) -> Kind {
    match token.to_ascii_lowercase().as_str() {
        "anthropic" | "anthropic_messages" => Kind::Anthropic,
        "openai" | "openai_chat_completions" | "openai-chat-completions" => Kind::OpenAiChat,
        "systemone" => Kind::Systemone,
        other => Kind::Other(other.to_string()),
    }
}

fn parse_provider(item: &Value, env: Env) -> Option<Provider> {
    let type_token = json_str(item, "type").or_else(|| json_str(item, "provider"))?;
    let kind = parse_kind(&type_token);
    let name = json_str(item, "name").unwrap_or_else(|| type_token.clone());
    // There is no managed Anthropic key concept: `ANTHROPIC_API_KEY` is never read, even when
    // a provider names it (router/AGENTS.md "Never set `ANTHROPIC_API_KEY`").
    let key_env = json_str(item, "apiKeyEnv")
        .or_else(|| json_str(item, "apikeyEnv"))
        .filter(|e| e != "ANTHROPIC_API_KEY");
    let apikey = json_str(item, "apikey")
        .or_else(|| json_str(item, "apiKey"))
        .or_else(|| key_env.and_then(|e| read_str(env(&e))));
    let baseurl = json_str(item, "baseurl")
        .or_else(|| json_str(item, "baseUrl"))
        .map(|b| b.trim_end_matches('/').to_string());
    let models = match item.get("models") {
        Some(Value::Array(a)) => a
            .iter()
            .filter_map(Value::as_str)
            .map(|s| s.trim().to_string())
            .collect(),
        Some(Value::String(s)) => list(Some(s.clone())),
        _ => Vec::new(),
    };
    let native_messages = kind == Kind::OpenAiChat
        && json_str(item, LOCAL_LEG_KEY).as_deref() == Some(LOCAL_LEG_NATIVE_MESSAGES);
    Some(Provider {
        name,
        kind,
        apikey,
        baseurl,
        models,
        native_messages,
        effort_kwargs: item.get("effortKwargs").cloned().unwrap_or(Value::Null),
        max_output_tokens: item.get("maxOutputTokens").cloned().unwrap_or(Value::Null),
    })
}

pub fn from_json(raw: &Value, env: Env) -> Result<Settings, String> {
    if !raw.is_object() {
        return Err("Gateway config payload must be a top-level JSON object.".into());
    }
    let providers = raw
        .get("providers")
        .or_else(|| raw.get("Providers"))
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|p| parse_provider(p, env))
                .map(Arc::new)
                .collect()
        })
        .unwrap_or_default();
    let host = read_str(env("HOST"))
        .or_else(|| json_str(raw, "host"))
        .unwrap_or_else(|| "0.0.0.0".into());
    let port = read_str(env("PORT"))
        .and_then(|p| p.parse::<f64>().ok())
        .or_else(|| raw.get("port").and_then(json_num))
        .unwrap_or(3000.0);
    let body_limit = [env("GATEWAY_BODY_LIMIT_BYTES"), env("BODY_LIMIT_BYTES")]
        .into_iter()
        .filter_map(|v| read_str(v).and_then(|s| s.parse::<f64>().ok()))
        .chain(
            ["bodyLimitBytes", "bodyLimit"]
                .iter()
                .filter_map(|k| raw.get(*k).and_then(json_num)),
        )
        .find(|n| *n >= 1.0)
        .map(|n| n as usize)
        .unwrap_or(DEFAULT_BODY_LIMIT);
    let upstream_timeout_ms = read_str(env("UPSTREAM_TIMEOUT_MS"))
        .and_then(|s| s.parse::<f64>().ok())
        .or_else(|| raw.get("upstreamTimeoutMs").and_then(json_num))
        .filter(|n| *n >= 0.0)
        .unwrap_or(0.0) as u64;
    let auth_raw = raw.get("auth").cloned().unwrap_or(Value::Null);
    let static_raw = auth_raw
        .get("staticApiKeys")
        .cloned()
        .unwrap_or(Value::Null);
    let mut keys = list(env("AUTH_STATIC_API_KEYS").or_else(|| env("AUTH_STATIC_API_KEY")));
    let key_env = read_str(env("AUTH_STATIC_API_KEY_ENV"))
        .or_else(|| read_str(env("AUTH_STATIC_API_KEYS_ENV")));
    for k in key_env.map(|e| list(env(&e))).unwrap_or_default() {
        if !keys.contains(&k) {
            keys.push(k);
        }
    }
    let auth_enabled = env_bool(
        env("AUTH_ENABLED"),
        auth_raw
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    );
    let mode = read_str(env("AUTH_MODE"))
        .or_else(|| json_str(&auth_raw, "mode"))
        .unwrap_or_default();
    if auth_enabled
        && !matches!(
            mode.to_ascii_lowercase().replace('-', "_").as_str(),
            "static_api_key" | "static_api_keys" | "static" | "api_key"
        )
    {
        return Err(format!(
            "AUTH_MODE {mode:?} is not supported; only static_api_key is."
        ));
    }
    let auth = AuthConfig {
        enabled: auth_enabled,
        required: env_bool(
            env("AUTH_REQUIRED"),
            auth_raw
                .get("required")
                .and_then(Value::as_bool)
                .unwrap_or(true),
        ),
        keys,
        header: read_str(env("AUTH_STATIC_API_KEY_HEADER"))
            .or_else(|| json_str(&static_raw, "keyHeader"))
            .unwrap_or_else(|| "authorization".into())
            .to_ascii_lowercase(),
        bearer_only: env_bool(
            env("AUTH_STATIC_API_KEY_BEARER_ONLY"),
            static_raw
                .get("keyBearerOnly")
                .and_then(Value::as_bool)
                .unwrap_or(true),
        ),
    };
    Ok(Settings {
        host,
        port: port as u16,
        providers,
        auth,
        body_limit,
        upstream_timeout_ms,
        anthropic_version: read_str(env("ANTHROPIC_VERSION"))
            .unwrap_or_else(|| "2023-06-01".into()),
    })
}

/// `GATEWAY_CONFIG_PATH`, else `./gateway.config.json` (Node `resolveGatewayConfigPath`).
pub fn load(env: Env) -> Result<Settings, String> {
    let path = read_str(env("GATEWAY_CONFIG_PATH")).unwrap_or_else(|| "gateway.config.json".into());
    let text = std::fs::read_to_string(&path).map_err(|e| format!("read {path}: {e}"))?;
    let raw: Value = serde_json::from_str(&text).map_err(|e| format!("parse {path}: {e}"))?;
    from_json(&raw, env)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |k| m.get(k).cloned()
    }

    #[test]
    fn env_beats_config_and_api_key_env_resolves() {
        let raw = serde_json::json!({"host":"0.0.0.0","port":4813,"providers":[
            {"name":"or","type":"openai_chat_completions","apiKeyEnv":"OR_KEY","baseurl":"https://x/v1/","models":["m"],"consumers":{"m":["router"]}},
            {"name":"anthropic","type":"anthropic","baseurl":"https://api.anthropic.com"},
            {"name":"nokind"}]});
        let env = env_of(&[
            ("HOST", "127.0.0.1"),
            ("PORT", "4999"),
            ("OR_KEY", "k1"),
            ("AUTH_STATIC_API_KEYS", "a, b"),
            ("AUTH_STATIC_API_KEY_HEADER", "X-Gateway-Key"),
            ("AUTH_STATIC_API_KEY_BEARER_ONLY", "false"),
            ("AUTH_ENABLED", "true"),
            ("AUTH_MODE", "static_api_key"),
        ]);
        let s = from_json(&raw, &env).unwrap();
        assert_eq!((s.host.as_str(), s.port), ("127.0.0.1", 4999));
        assert_eq!(s.providers.len(), 2);
        assert_eq!(s.providers[0].apikey.as_deref(), Some("k1"));
        assert_eq!(s.providers[0].baseurl.as_deref(), Some("https://x/v1"));
        assert_eq!(s.providers[1].apikey, None);
        assert_eq!(s.auth.keys, vec!["a", "b"]);
        assert_eq!(s.auth.header, "x-gateway-key");
        assert!(!s.auth.bearer_only && s.auth.enabled && s.auth.required);
        assert_eq!((s.body_limit, s.upstream_timeout_ms), (50 * 1024 * 1024, 0));
        let s = from_json(&raw, &env_of(&[])).unwrap();
        assert_eq!((s.host.as_str(), s.port), ("0.0.0.0", 4813));
        assert_eq!(s.providers[0].apikey, None);
        assert!(!s.auth.enabled && s.auth.bearer_only);
    }

    #[test]
    fn production_config_parses_with_selector() {
        let text = include_str!("../../gateway.config.json");
        let raw: Value = serde_json::from_str(text).unwrap();
        let s = from_json(&raw, &env_of(&[])).unwrap();
        let local = s
            .providers
            .iter()
            .find(|p| p.name == "claude-local")
            .unwrap();
        assert!(local.native_messages && local.apikey.is_some());
        assert!(local.effort_kwargs.is_object() && local.max_output_tokens.is_object());
        assert!(s
            .providers
            .iter()
            .filter(|p| p.name != "claude-local")
            .all(|p| !p.native_messages));
    }
}
