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
/// The closed `consumers` vocabulary (the values present in the legacy config at WP-2).
pub const CONSUMERS: [&str; 2] = ["cc", "router"];
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
}

/// A validated catalog: the routing providers (static provider + catalog model fields, in
/// static config order) and the content revision.
#[derive(Debug)]
pub struct Catalog {
    pub revision: String,
    pub providers: Vec<Arc<Provider>>,
    pub entries: Vec<(String, Vec<Entry>)>,
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
        for c in e.consumers.iter().flatten() {
            if !CONSUMERS.contains(&c.as_str()) {
                return Err(format!(
                    "model {:?}: consumer {c:?} is not one of {CONSUMERS:?}",
                    e.id
                ));
            }
        }
    }
    Ok(())
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
    Ok(Catalog {
        revision: revision(bytes),
        providers,
        entries,
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

/// Derive a catalog file from the legacy per-model keys of a parsed config. Test fixtures and
/// the B1 equality guard only; never a routing source in the server. Deleted in WP-3.
#[doc(hidden)]
pub fn legacy_catalog_json(settings: &crate::config::Settings) -> Value {
    let mut providers = Map::new();
    for l in &settings.legacy {
        let models: Vec<Value> = l
            .models
            .iter()
            .map(|m| {
                let mut e = Map::new();
                e.insert("id".into(), Value::String(m.clone()));
                if let Some(k) = l.effort_kwargs.get(m) {
                    e.insert(
                        "effortKwargs".into(),
                        serde_json::to_value(k).unwrap_or_default(),
                    );
                }
                if let Some(t) = l.max_output_tokens.get(m) {
                    e.insert("maxOutputTokens".into(), Value::from(*t));
                }
                let consumers = match &l.consumers {
                    Some(Value::Object(map)) => map.get(m).cloned(),
                    Some(list @ Value::Array(_)) => Some(list.clone()),
                    _ => None,
                };
                if let Some(c) = consumers {
                    e.insert("consumers".into(), c);
                }
                Value::Object(e)
            })
            .collect();
        let mut p = Map::new();
        p.insert("models".into(), Value::Array(models));
        providers.insert(l.name.clone(), Value::Object(p));
    }
    let mut root = Map::new();
    root.insert("schema".into(), Value::String(SCHEMA.into()));
    root.insert("providers".into(), Value::Object(providers));
    Value::Object(root)
}

/// A non-reloading holder from the legacy keys (test fixtures only; deleted in WP-3).
#[doc(hidden)]
pub fn legacy_holder(settings: &crate::config::Settings) -> Result<Arc<Holder>, String> {
    let bytes = legacy_catalog_json(settings).to_string().into_bytes();
    let cat = check(&settings.providers, &bytes)?;
    Ok(Arc::new(Holder::new(settings.providers.clone(), cat, None)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::from_json;

    fn keys(k: &str) -> Option<String> {
        (k == "AUTH_STATIC_API_KEYS").then(|| "gk".to_string())
    }

    fn settings() -> crate::config::Settings {
        let text = include_str!("../../gateway.config.json");
        from_json(&serde_json::from_str(text).unwrap(), &keys).unwrap()
    }

    const CATALOG: &str = include_str!("../../catalog/catalog.json");

    /// B1 guard: the committed catalog equals the legacy per-model keys exactly, provider by
    /// provider, in order. WP-3 deletes this test with the legacy keys.
    #[test]
    fn committed_catalog_equals_legacy_keys() {
        let s = settings();
        let cat = check(&s.providers, CATALOG.as_bytes()).unwrap();
        let committed: Value = serde_json::from_str(CATALOG).unwrap();
        assert_eq!(committed, legacy_catalog_json(&s));
        let names: Vec<_> = committed["providers"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        let static_names: Vec<_> = s.providers.iter().map(|p| p.name.clone()).collect();
        assert_eq!(names, static_names);
        for (p, l) in cat.providers.iter().zip(&s.legacy) {
            assert_eq!(p.models, l.models, "{}", p.name);
            assert_eq!(p.effort_kwargs, l.effort_kwargs, "{}", p.name);
            assert_eq!(p.max_output_tokens, l.max_output_tokens, "{}", p.name);
        }
    }

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
            serde_json::json!([{"id": "a", "consumers": ["openwebui"]}]),
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
}
