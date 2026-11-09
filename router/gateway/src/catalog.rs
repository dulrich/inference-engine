//! The model catalog (`router/catalog/catalog.json`, schema `model-catalog-v1`): the only
//! routing source for model ids, `effortKwargs` and `maxOutputTokens`. Hot-reloaded by polling
//! the file (works across rename-into-directory replacement), validate-then-swap; an invalid
//! candidate is rejected and the last good catalog stays live. Each request takes one `Arc`
//! snapshot at entry, so in-flight requests finish on the catalog they started with.

use crate::config::{check_effort_levels, EffortKwargs, Env, MaxOutputTokens, Provider};
use serde::Deserialize;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::num::NonZeroU64;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

pub const SCHEMA: &str = "model-catalog-v1";
/// The closed `consumers` vocabulary, in the canonical order `control/serving_protocol.py` uses
/// (`cc`, `openwebui`), then the gateway-only `router` tag.
pub const CONSUMERS: [&str; 3] = ["cc", "openwebui", "router"];
/// Providers that accept the OpenRouter `@preset/<slug>` id form (measured, WP-0).
pub const PRESET_PROVIDERS: [&str; 2] = ["openrouter", "or-anthropic"];
pub const POLL_INTERVAL: Duration = Duration::from_secs(1);
const DEFAULT_PATH: &str = "catalog/catalog.json";

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    pub id: String,
    #[serde(default, rename = "effortKwargs")]
    pub effort_kwargs: Option<HashMap<String, Map<String, Value>>>,
    #[serde(default, rename = "maxOutputTokens")]
    pub max_output_tokens: Option<NonZeroU64>,
    #[serde(default)]
    pub consumers: Option<Vec<String>>,
    /// The `llama/config.yaml` model key this id serves (`claude-local` only); the serving
    /// dispatcher addresses visibility by it. Never a routing input.
    #[serde(default, rename = "modelKey")]
    pub model_key: Option<String>,
    /// Display label for catalog editors; the gateway ignores it.
    #[serde(default)]
    pub label: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderEntry {
    pub models: Vec<Entry>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogFile {
    pub schema: String,
    /// Keyed by static provider name (JSON object order is kept but routing order is the
    /// static config's).
    pub providers: serde_json::Map<String, Value>,
    /// Visibility records for llama model keys with no gateway id, keyed by model key. Parsed
    /// strictly, never routed on.
    #[serde(default)]
    pub exposures: Map<String, Value>,
}

/// One exposure record: a llama model key the gateway does not route, with its consumers.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Exposure {
    pub consumers: Vec<String>,
}

/// The provider whose entries may carry `modelKey`.
pub const MODEL_KEY_PROVIDER: &str = "claude-local";

/// A validated catalog: the routing providers (static provider + catalog model fields, in
/// static config order) and the content revision.
#[derive(Debug)]
pub struct Catalog {
    pub revision: String,
    pub providers: Vec<Arc<Provider>>,
    pub entries: Vec<(String, Vec<Entry>)>,
    /// Exposure records in file order (never routed on).
    pub exposures: Vec<(String, Exposure)>,
}

/// FNV-1a 64 over the file bytes, 16 hex digits: stable across processes and builds.
pub fn revision(bytes: &[u8]) -> String {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in bytes {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    format!("{h:016x}")
}

fn preset_slug_ok(slug: &str) -> bool {
    let mut chars = slug.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase() || c.is_ascii_digit())
        && chars
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '_' | '-'))
}

fn check_id(provider: &str, id: &str) -> Result<(), String> {
    if id.is_empty() || id.trim() != id {
        return Err(format!(
            "model id {id:?} is empty or has surrounding whitespace"
        ));
    }
    if let Some(rest) = id.strip_prefix('@') {
        let slug = rest.strip_prefix("preset/");
        if !slug.is_some_and(preset_slug_ok) {
            return Err(format!(
                "model id {id:?}: an '@' id must be @preset/<slug> with slug [a-z0-9][a-z0-9._-]*"
            ));
        }
        if !PRESET_PROVIDERS.contains(&provider) {
            return Err(format!(
                "model id {id:?}: @preset ids are accepted only on {PRESET_PROVIDERS:?}"
            ));
        }
    }
    Ok(())
}

fn check_provider(p: &Provider, entries: &[Entry]) -> Result<(), String> {
    if entries.is_empty() && p.credentialed {
        return Err(
            "models[] is empty on a credentialed provider (an empty list would be a wildcard)"
                .into(),
        );
    }
    let mut seen: Vec<&str> = Vec::new();
    for e in entries {
        check_id(&p.name, &e.id)?;
        if seen.contains(&e.id.as_str()) {
            return Err(format!("duplicate model id {:?}", e.id));
        }
        seen.push(&e.id);
        if let Some(levels) = &e.effort_kwargs {
            check_effort_levels(&e.id, levels)?;
        }
        check_consumers(&format!("model {:?}", e.id), e.consumers.iter().flatten())?;
        if let Some(k) = &e.model_key {
            if p.name != MODEL_KEY_PROVIDER {
                return Err(format!(
                    "model {:?}: modelKey is accepted only on {MODEL_KEY_PROVIDER:?}",
                    e.id
                ));
            }
            check_model_key(k)?;
            if e.consumers.is_none() {
                return Err(format!(
                    "model {:?}: an entry with a modelKey must carry consumers",
                    e.id
                ));
            }
        }
    }
    Ok(())
}

fn check_consumers<'a>(
    what: &str,
    consumers: impl Iterator<Item = &'a String>,
) -> Result<(), String> {
    for c in consumers {
        if !CONSUMERS.contains(&c.as_str()) {
            return Err(format!(
                "{what}: consumer {c:?} is not one of {CONSUMERS:?}"
            ));
        }
    }
    Ok(())
}

fn check_model_key(k: &str) -> Result<(), String> {
    if k.is_empty() || k.trim() != k {
        return Err(format!(
            "model key {k:?} is empty or has surrounding whitespace"
        ));
    }
    Ok(())
}

/// Exposure records: strict shape, known consumers, no `cc` (a picker row needs a gateway id),
/// and no model key shared with a routed entry.
fn check_exposures(
    raw: &Map<String, Value>,
    entries: &[(String, Vec<Entry>)],
) -> Result<Vec<(String, Exposure)>, String> {
    let mut keys: Vec<&str> = Vec::new();
    for (_, models) in entries {
        for k in models.iter().filter_map(|e| e.model_key.as_deref()) {
            if keys.contains(&k) {
                return Err(format!("duplicate model key {k:?}"));
            }
            keys.push(k);
        }
    }
    let mut out = Vec::new();
    for (key, v) in raw {
        let x = Exposure::deserialize(v).map_err(|e| format!("exposure {key:?}: {e}"))?;
        check_model_key(key)?;
        if keys.contains(&key.as_str()) {
            return Err(format!(
                "exposure {key:?}: model key is already on a routed entry"
            ));
        }
        let what = format!("exposure {key:?}");
        check_consumers(&what, x.consumers.iter())?;
        if x.consumers.iter().any(|c| c == "cc") {
            return Err(format!("{what}: consumer \"cc\" needs a routed gateway id"));
        }
        out.push((key.clone(), x));
    }
    Ok(out)
}

/// Parse and validate a candidate against the static providers.
pub fn check(static_providers: &[Arc<Provider>], bytes: &[u8]) -> Result<Catalog, String> {
    let file: CatalogFile = serde_json::from_slice(bytes).map_err(|e| format!("catalog: {e}"))?;
    if file.schema != SCHEMA {
        return Err(format!(
            "catalog: schema {:?} is not {SCHEMA:?}",
            file.schema
        ));
    }
    if let Some(name) = file
        .providers
        .keys()
        .find(|n| !static_providers.iter().any(|p| &p.name == *n))
    {
        return Err(format!(
            "catalog provider {name:?} names no provider in the static config"
        ));
    }
    let mut providers = Vec::new();
    let mut entries = Vec::new();
    for p in static_providers {
        let raw = file
            .providers
            .get(&p.name)
            .ok_or_else(|| format!("catalog has no entry for static provider {:?}", p.name))?;
        let pe = ProviderEntry::deserialize(raw)
            .map_err(|e| format!("catalog provider {:?}: {e}", p.name))?;
        check_provider(p, &pe.models).map_err(|e| format!("catalog provider {:?}: {e}", p.name))?;
        let mut routed = Provider::clone(p);
        routed.models = pe.models.iter().map(|e| e.id.clone()).collect();
        routed.effort_kwargs = pe
            .models
            .iter()
            .filter_map(|e| Some((e.id.clone(), e.effort_kwargs.clone()?)))
            .collect::<EffortKwargs>();
        routed.max_output_tokens = pe
            .models
            .iter()
            .filter_map(|e| Some((e.id.clone(), e.max_output_tokens?.get())))
            .collect::<MaxOutputTokens>();
        providers.push(Arc::new(routed));
        entries.push((p.name.clone(), pe.models));
    }
    let exposures = check_exposures(&file.exposures, &entries)?;
    Ok(Catalog {
        revision: revision(bytes),
        providers,
        entries,
        exposures,
    })
}

/// `GATEWAY_CATALOG_PATH`, else `catalog/catalog.json` relative to the working directory.
pub fn path_from_env(env: Env) -> PathBuf {
    crate::env::trimmed(env("GATEWAY_CATALOG_PATH"))
        .unwrap_or_else(|| DEFAULT_PATH.into())
        .into()
}

/// Read + check one file (startup and `check-catalog`).
pub fn load_file(static_providers: &[Arc<Provider>], path: &PathBuf) -> Result<Catalog, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("read catalog {}: {e}", path.display()))?;
    check(static_providers, &bytes).map_err(|e| format!("{}: {e}", path.display()))
}

#[derive(Default)]
struct Status {
    /// The current rejection reason (`/health` `catalogError`).
    error: Option<String>,
    /// The last rejected revision (or read error), so a bad file is checked and logged once.
    rejected: Option<String>,
}

/// The live catalog holder: an atomically swapped `Arc<Catalog>` plus the reload status.
pub struct Holder {
    current: RwLock<Arc<Catalog>>,
    status: Mutex<Status>,
    static_providers: Vec<Arc<Provider>>,
    path: Option<PathBuf>,
}

impl Holder {
    /// A holder that reloads from `path` (see `spawn_reloader`).
    pub fn new(
        static_providers: Vec<Arc<Provider>>,
        initial: Catalog,
        path: Option<PathBuf>,
    ) -> Self {
        Holder {
            current: RwLock::new(Arc::new(initial)),
            status: Mutex::default(),
            static_providers,
            path,
        }
    }

    /// Startup load: a missing or invalid catalog is a fatal error.
    pub fn load(static_providers: Vec<Arc<Provider>>, path: PathBuf) -> Result<Self, String> {
        let initial = load_file(&static_providers, &path)?;
        Ok(Self::new(static_providers, initial, Some(path)))
    }

    /// The per-request snapshot: clone once at request entry.
    pub fn snapshot(&self) -> Arc<Catalog> {
        match self.current.read() {
            Ok(g) => g.clone(),
            Err(p) => p.into_inner().clone(),
        }
    }

    pub fn error(&self) -> Option<String> {
        self.status.lock().map(|s| s.error.clone()).unwrap_or(None)
    }

    fn swap(&self, next: Catalog) {
        let next = Arc::new(next);
        match self.current.write() {
            Ok(mut g) => *g = next,
            Err(p) => *p.into_inner() = next,
        }
    }

    /// One reload step: re-read the file; swap on a new valid revision; on an invalid one keep
    /// the last good catalog, set `catalogError`, and log once per distinct revision.
    pub fn poll_once(&self) {
        let Some(path) = &self.path else { return };
        let Ok(mut st) = self.status.lock() else {
            return;
        };
        let bytes = match std::fs::read(path) {
            Ok(b) => b,
            Err(e) => {
                let reason = format!("read catalog {}: {e}", path.display());
                if st.rejected.as_deref() != Some(reason.as_str()) {
                    eprintln!(
                        "inference-router: catalog reload rejected, last good stays live: {reason}"
                    );
                    st.rejected = Some(reason.clone());
                }
                st.error = Some(reason);
                return;
            }
        };
        let rev = revision(&bytes);
        if rev == self.snapshot().revision {
            st.error = None;
            st.rejected = None;
            return;
        }
        if st.rejected.as_deref() == Some(rev.as_str()) {
            return;
        }
        match check(&self.static_providers, &bytes) {
            Ok(next) => {
                eprintln!("inference-router: catalog revision {rev} live");
                self.swap(next);
                st.error = None;
                st.rejected = None;
            }
            Err(reason) => {
                eprintln!(
                    "inference-router: catalog revision {rev} rejected, last good stays live: {reason}"
                );
                st.error = Some(reason);
                st.rejected = Some(rev);
            }
        }
    }

    /// Poll the catalog file every `interval` (no-op without a path). Must run inside a tokio
    /// runtime.
    pub fn spawn_reloader(self: &Arc<Self>, interval: Duration) {
        if self.path.is_none() {
            return;
        }
        let me = Arc::downgrade(self);
        tokio::spawn(async move {
            let mut iv = tokio::time::interval(interval);
            iv.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                iv.tick().await;
                let Some(h) = me.upgrade() else { break };
                h.poll_once();
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::from_json;
    use serde_json::json;

    fn keys(k: &str) -> Option<String> {
        (k == "AUTH_STATIC_API_KEYS").then(|| "gk".to_string())
    }

    fn settings() -> crate::config::Settings {
        let text = include_str!("../../test/fixtures/gateway.config.fixture.json");
        from_json(&serde_json::from_str(text).unwrap(), &keys).unwrap()
    }

    const CATALOG: &str = include_str!("../../test/fixtures/catalog.router.fixture.json");

    fn with(provider: &str, models: Value) -> Result<Catalog, String> {
        let s = settings();
        let mut v: Value = serde_json::from_str(CATALOG).unwrap();
        v["providers"][provider]["models"] = models;
        check(&s.providers, v.to_string().as_bytes())
    }

    #[test]
    fn empty_models_on_a_credentialed_provider_is_rejected() {
        let e = with("openrouter", serde_json::json!([])).unwrap_err();
        assert!(e.contains("empty on a credentialed provider"), "{e}");
    }

    #[test]
    fn preset_ids_only_on_openrouter_legs() {
        let ok = with("openrouter", serde_json::json!([{"id": "@preset/x"}])).unwrap();
        let or = ok
            .providers
            .iter()
            .find(|p| p.name == "openrouter")
            .unwrap();
        assert_eq!(or.models, ["@preset/x"]);
        assert!(with(
            "or-anthropic",
            serde_json::json!([{"id": "@preset/a.b_c-1"}])
        )
        .is_ok());
        let e = with("claude-local", serde_json::json!([{"id": "@preset/x"}])).unwrap_err();
        assert!(e.contains("accepted only on"), "{e}");
        let e = with("openrouter", serde_json::json!([{"id": "@preset/Bad"}])).unwrap_err();
        assert!(e.contains("@preset/<slug>"), "{e}");
    }

    #[test]
    fn rejects_duplicates_unknown_providers_consumers_and_levels() {
        let e = with("openrouter", serde_json::json!([{"id": "a"}, {"id": "a"}])).unwrap_err();
        assert!(e.contains("duplicate model id"), "{e}");
        let e = with(
            "openrouter",
            serde_json::json!([{"id": "a", "consumers": ["webui"]}]),
        )
        .unwrap_err();
        assert!(e.contains("is not one of"), "{e}");
        let e = with(
            "openrouter",
            serde_json::json!([{"id": "a", "effortKwargs": {"huge": {}}}]),
        )
        .unwrap_err();
        assert!(e.contains("level \"huge\""), "{e}");
        let e = with("openrouter", serde_json::json!([{"id": "a", "bogus": 1}])).unwrap_err();
        assert!(e.contains("unknown field `bogus`"), "{e}");
        let s = settings();
        let mut v: Value = serde_json::from_str(CATALOG).unwrap();
        v["providers"]["nope"] = serde_json::json!({"models": [{"id": "m"}]});
        let e = check(&s.providers, v.to_string().as_bytes()).unwrap_err();
        assert!(e.contains("names no provider"), "{e}");
        v["providers"].as_object_mut().unwrap().remove("nope");
        v["providers"].as_object_mut().unwrap().remove("anthropic");
        let e = check(&s.providers, v.to_string().as_bytes()).unwrap_err();
        assert!(e.contains("no entry for static provider"), "{e}");
    }

    #[test]
    fn revision_is_stable_hex() {
        assert_eq!(revision(b""), "cbf29ce484222325");
        assert_eq!(revision(CATALOG.as_bytes()).len(), 16);
    }

    #[test]
    fn committed_exposures_parse_and_are_never_routed() {
        let s = settings();
        let cat = check(&s.providers, CATALOG.as_bytes()).unwrap();
        // Invariants only, never live catalog visibility (precedent 04a0511): every committed
        // exposure record parses, and no exposure key is a provider model -- the set both routing
        // and `/v1/models` (`relay::models`, `provider/model`) are built from.
        let raw: Value = serde_json::from_str(CATALOG).unwrap();
        let raw_len = raw["exposures"].as_object().map_or(0, |m| m.len());
        assert_eq!(cat.exposures.len(), raw_len);
        let routed: Vec<&String> = cat.providers.iter().flat_map(|p| &p.models).collect();
        for (key, _) in &cat.exposures {
            assert!(!routed.contains(&key), "{key} is routed");
        }
        let local = &cat
            .entries
            .iter()
            .find(|(n, _)| n == "claude-local")
            .unwrap()
            .1;
        assert!(local
            .iter()
            .all(|e| e.model_key.is_some() && e.consumers.is_some()));
    }

    #[test]
    fn openwebui_is_a_consumer_and_model_key_rules_hold() {
        let ok = with(
            "openrouter",
            json!([{"id": "a", "consumers": ["cc", "openwebui", "router"]}]),
        );
        assert!(ok.is_ok(), "{ok:?}");
        let e = with(
            "openrouter",
            json!([{"id": "a", "modelKey": "k", "consumers": []}]),
        )
        .unwrap_err();
        assert!(e.contains("modelKey is accepted only on"), "{e}");
        let e = with("claude-local", json!([{"id": "a", "modelKey": "k"}])).unwrap_err();
        assert!(e.contains("must carry consumers"), "{e}");
        let e = with(
            "claude-local",
            json!([{"id": "a", "modelKey": "k", "consumers": []}, {"id": "b", "modelKey": "k", "consumers": []}]),
        )
        .unwrap_err();
        assert!(e.contains("duplicate model key"), "{e}");
    }

    #[test]
    fn exposure_records_are_strict() {
        let s = settings();
        let base: Value = serde_json::from_str(CATALOG).unwrap();
        let try_exp = |key: &str, rec: Value| {
            let mut v = base.clone();
            v["exposures"][key] = rec;
            check(&s.providers, v.to_string().as_bytes())
        };
        assert!(try_exp("new-key", json!({"consumers": []})).is_ok());
        let e = try_exp("new-key", json!({"consumers": ["webui"]})).unwrap_err();
        assert!(e.contains("is not one of"), "{e}");
        let e = try_exp("new-key", json!({"consumers": ["cc"]})).unwrap_err();
        assert!(e.contains("needs a routed gateway id"), "{e}");
        let e = try_exp("new-key", json!({"consumers": [], "gateway_id": null})).unwrap_err();
        assert!(e.contains("unknown field `gateway_id`"), "{e}");
        let e = try_exp("nemotron-3-nano-4B-UD-Q4_K_XL", json!({"consumers": []})).unwrap_err();
        assert!(e.contains("already on a routed entry"), "{e}");
    }
}
