//! Model → ordered targets, following Node `resolveTargetProviders`
//! (`src/gateway/handler.ts:5394`), `parseModelReference` (`:5725`), `validateModelForTarget`
//! (`:5528`) and `resolveDefaultTargetProviders` (`src/config.ts:4930`), then filtered to the
//! providers that speak the ingress protocol (Decision 2).

use crate::config::{Kind, Provider, Settings};
use serde_json::{json, Map, Value};
use std::sync::Arc;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ingress {
    Messages,
    CountTokens,
    Chat,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Leg {
    /// Anthropic-protocol provider (`anthropic`, `or-anthropic`, `codex-*`).
    Anthropic,
    /// `openai_chat_completions` provider with the native-messages selector, on Messages ingress.
    LocalMessages,
    /// OpenAI chat provider on chat ingress.
    OpenAiChat,
}

#[derive(Clone, Debug)]
pub struct Candidate {
    pub provider: Arc<Provider>,
    pub model: String,
    pub leg: Leg,
}

/// One failed attempt, rendered into Node's `All target providers failed.` envelope.
#[derive(Clone, Debug)]
pub struct Attempt {
    pub provider: Arc<Provider>,
    pub stage: &'static str,
    pub message: String,
    pub status: Option<u16>,
    pub details: Option<Value>,
}

/// One target in Node's ordered route list: either ready to call or already failed (model
/// validation / selector conflict) — a failed one still counts as "a next target exists".
pub struct Target {
    pub provider: Arc<Provider>,
    /// Named explicitly (model prefix or provider-name header) vs a default/type route; Node
    /// lists only explicit ones in `target_provider_names`.
    pub explicit: bool,
    pub plan: Result<Candidate, Attempt>,
}

pub enum Resolved {
    /// Targets in fallthrough order, already filtered to the ingress protocol.
    Targets(Vec<Target>),
    /// The model resolves only to providers that do not speak this ingress protocol.
    Mismatch { model: String, provider: String },
    /// Node `resolveTargetProviders` 400s (bad / conflicting selector headers).
    BadRequest(String),
}

/// Client routing headers (Node `handler.ts:5408-5450`).
#[derive(Default, Clone, Copy)]
pub struct Selectors<'a> {
    /// `x-target-providers`: comma list of provider names or `openai`/`anthropic`.
    pub providers: Option<&'a str>,
    /// `x-target-provider`: one provider name or type.
    pub provider: Option<&'a str>,
}

fn leg_for(p: &Provider, ingress: Ingress) -> Option<Leg> {
    match (&p.kind, ingress) {
        (Kind::Anthropic, Ingress::Messages | Ingress::CountTokens) => Some(Leg::Anthropic),
        (Kind::OpenAiChat, Ingress::Chat) => Some(Leg::OpenAiChat),
        (Kind::OpenAiChat, _) if p.native_messages => Some(Leg::LocalMessages),
        _ => None,
    }
}

/// Provider by name (case-insensitive) → explicit; else `openai`/`anthropic` → the first
/// provider of that kind as a type route (Node `parseProviderRoute` `handler.ts:5654`).
fn route_by(s: &Settings, sel: &str) -> Option<(Arc<Provider>, bool)> {
    let sel = sel.trim();
    if let Some(p) = s
        .providers
        .iter()
        .find(|p| !sel.is_empty() && p.name.eq_ignore_ascii_case(sel))
    {
        return Some((p.clone(), true));
    }
    let kind = match sel.to_ascii_lowercase().as_str() {
        "anthropic" => Kind::Anthropic,
        "openai" => Kind::OpenAiChat,
        _ => return None,
    };
    s.providers
        .iter()
        .find(|p| p.kind == kind)
        .map(|p| (p.clone(), false))
}

/// Node `parseModelReference`: `(provider prefix route, model)`.
fn model_ref(s: &Settings, raw: &str) -> (Option<(Arc<Provider>, bool)>, String) {
    if let Some((hint, model)) = raw
        .split_once('/')
        .filter(|(h, m)| !h.trim().is_empty() && !m.trim().is_empty())
    {
        if let Some(r) = route_by(s, hint) {
            return (Some(r), model.trim().to_string());
        }
    }
    (None, raw.to_string())
}

/// First provider per kind, config order, never `systemone`.
fn default_routes(s: &Settings) -> Vec<(Arc<Provider>, bool)> {
    let mut seen: Vec<&Kind> = Vec::new();
    let mut out = Vec::new();
    for p in s.providers.iter().filter(|p| p.kind != Kind::Systemone) {
        if !seen.contains(&&p.kind) {
            seen.push(&p.kind);
            out.push((p.clone(), false));
        }
    }
    out
}

fn allowed(s: &Settings) -> String {
    let mut v = vec!["openai".to_string(), "anthropic".to_string()];
    v.extend(s.providers.iter().map(|p| p.name.clone()));
    v.join(", ")
}

fn validates(p: &Provider, model: &str) -> bool {
    p.models.is_empty() || p.models.iter().any(|m| m == model)
}

pub fn resolve(s: &Settings, ingress: Ingress, requested: &str, sel: Selectors<'_>) -> Resolved {
    let requested = requested.trim();
    let (mref, model) = model_ref(s, requested);
    let mref = mref.map(|(p, _)| p);
    let conflicts = |p: &Arc<Provider>| mref.as_ref().is_some_and(|m| !Arc::ptr_eq(m, p));
    let routes = if let Some(list) = sel.providers {
        let mut r: Vec<(Arc<Provider>, bool)> = Vec::new();
        for (p, explicit) in list.split(',').filter_map(|i| route_by(s, i)) {
            if !r.iter().any(|(q, _)| Arc::ptr_eq(q, &p)) {
                r.push((p, explicit));
            }
        }
        if r.is_empty() {
            return Resolved::BadRequest(format!(
                "x-target-providers must include at least one valid provider: {}",
                allowed(s)
            ));
        }
        if mref.is_some() && r.iter().all(|(p, _)| conflicts(p)) {
            return Resolved::BadRequest(format!(
                "Model selector \"{requested}\" conflicts with x-target-providers."
            ));
        }
        r
    } else if let Some(one) = sel.provider {
        let Some(r) = route_by(s, one) else {
            return Resolved::BadRequest(format!(
                "x-target-provider must be one of: {}",
                allowed(s)
            ));
        };
        if conflicts(&r.0) {
            return Resolved::BadRequest(format!(
                "Model selector \"{requested}\" conflicts with x-target-provider."
            ));
        }
        vec![r]
    } else if let Some(p) = &mref {
        vec![(p.clone(), true)]
    } else {
        default_routes(s)
    };
    let mut targets = Vec::new();
    let mut mismatch = None;
    for (p, explicit) in routes {
        let Some(leg) = leg_for(&p, ingress) else {
            if mismatch.is_none() && (validates(&p, &model) || p.kind == Kind::Systemone) {
                mismatch = Some(p.name.clone());
            }
            continue;
        };
        let fail = |message: String| Attempt {
            provider: p.clone(),
            stage: "model_resolution",
            message,
            status: Some(400),
            details: None,
        };
        let plan = if conflicts(&p) {
            Err(fail(format!(
                "model \"{requested}\" conflicts with target provider {}.",
                p.name
            )))
        } else if validates(&p, &model) {
            Ok(Candidate {
                provider: p.clone(),
                model: model.clone(),
                leg,
            })
        } else {
            Err(fail(format!(
                "Model \"{model}\" is not configured for target provider {}. Allowed models: {}.",
                p.name,
                p.models.join(", ")
            )))
        };
        targets.push(Target {
            provider: p,
            explicit,
            plan,
        });
    }
    match mismatch {
        Some(provider) if targets.iter().all(|t| t.plan.is_err()) => Resolved::Mismatch {
            model: requested.to_string(),
            provider,
        },
        _ => Resolved::Targets(targets),
    }
}

/// Node `buildFallbackErrorPayload` (`handler.ts:6261`); `undefined` fields are omitted as
/// `JSON.stringify` does.
pub fn envelope(targets: &[Target], attempts: &[Attempt]) -> (u16, Value) {
    let status = attempts
        .last()
        .and_then(|a| a.status)
        .filter(|s| (100..=599).contains(s))
        .unwrap_or(502);
    let attempts: Vec<Value> = attempts
        .iter()
        .map(|a| {
            let mut m = Map::new();
            m.insert("provider".into(), json!(a.provider.kind.token()));
            m.insert("provider_name".into(), json!(a.provider.name));
            m.insert("stage".into(), json!(a.stage));
            m.insert("message".into(), json!(a.message));
            if let Some(s) = a.status {
                m.insert("status".into(), json!(s));
            }
            if let Some(d) = &a.details {
                m.insert("details".into(), d.clone());
            }
            Value::Object(m)
        })
        .collect();
    let body = json!({"error": {
        "message": "All target providers failed.",
        "target_providers": targets.iter().map(|t| t.provider.kind.token()).collect::<Vec<_>>(),
        "target_provider_names": targets.iter().filter(|t| t.explicit).map(|t| t.provider.name.as_str()).collect::<Vec<_>>(),
        "attempts": attempts,
    }});
    (status, body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::from_json;

    fn settings() -> Settings {
        let text = include_str!("../../gateway.config.json");
        from_json(&serde_json::from_str(text).unwrap(), &|_| None).unwrap()
    }

    fn ok(r: &Resolved) -> Vec<(String, String, Leg)> {
        match r {
            Resolved::Targets(t) => t
                .iter()
                .filter_map(|t| t.plan.as_ref().ok())
                .map(|c| (c.provider.name.clone(), c.model.clone(), c.leg))
                .collect(),
            _ => vec![],
        }
    }

    #[test]
    fn selection_order_and_protocol_filter() {
        let s = settings();
        let n = |i, m| ok(&resolve(&s, i, m, Selectors::default()));
        let own = |a: &str, b: &str, l| vec![(a.to_string(), b.to_string(), l)];
        assert_eq!(
            n(Ingress::Messages, "claude-opus-5-5"),
            own("anthropic", "claude-opus-5-5", Leg::Anthropic)
        );
        assert_eq!(
            n(Ingress::Messages, "qwen38-27b-iq3s"),
            own("claude-local", "qwen38-27b-iq3s", Leg::LocalMessages)
        );
        assert_eq!(
            n(Ingress::Messages, "claude-local/qwen36-a3b"),
            own("claude-local", "qwen36-a3b", Leg::LocalMessages)
        );
        assert_eq!(
            n(Ingress::Chat, "claude-local/qwen36-a3b"),
            own("claude-local", "qwen36-a3b", Leg::OpenAiChat)
        );
        assert_eq!(
            n(Ingress::Messages, "codex-b/gpt-6-luna"),
            own("codex-b", "gpt-6-luna", Leg::Anthropic)
        );
        assert_eq!(
            n(Ingress::Messages, "or-anthropic/qwen/qwen3.8-flash"),
            own("or-anthropic", "qwen/qwen3.8-flash", Leg::Anthropic)
        );
        assert_eq!(
            n(Ingress::Chat, "openrouter/qwen/qwen3.8-flash"),
            own("openrouter", "qwen/qwen3.8-flash", Leg::OpenAiChat)
        );
        // Bare multi-provider id: the default list is first-provider-per-kind, so neither
        // openrouter nor or-anthropic is a bare-id target.
        assert!(n(Ingress::Messages, "qwen/qwen3.8-flash").is_empty());
        // Fallthrough across same-model providers only via x-target-providers.
        let sel = Selectors {
            providers: Some("codex-a, codex-b,nope"),
            provider: None,
        };
        let got = ok(&resolve(&s, Ingress::Messages, "gpt-6-luna", sel));
        assert_eq!(
            got.iter().map(|c| c.0.as_str()).collect::<Vec<_>>(),
            ["codex-a", "codex-b"]
        );
        assert!(matches!(
            resolve(
                &s,
                Ingress::Messages,
                "codex-b/gpt-6-luna",
                Selectors {
                    providers: Some("codex-a"),
                    provider: None
                }
            ),
            Resolved::BadRequest(_)
        ));
        assert!(matches!(
            resolve(
                &s,
                Ingress::Messages,
                "m",
                Selectors {
                    providers: Some("nope"),
                    provider: None
                }
            ),
            Resolved::BadRequest(_)
        ));
    }

    #[test]
    fn protocol_mismatch_refusals() {
        let s = settings();
        for (i, m) in [
            (Ingress::Messages, "openrouter/qwen/qwen3.8-flash"),
            (Ingress::Chat, "or-anthropic/z-ai/glm-5.3-flash"),
            (Ingress::Chat, "claude-opus-5-5"),
            (Ingress::Messages, "openrouter-decide/typesafe/jev-1.13"),
            (Ingress::CountTokens, "openrouter/qwen/qwen3.8-flash"),
        ] {
            assert!(
                matches!(
                    resolve(&s, i, m, Selectors::default()),
                    Resolved::Mismatch { .. }
                ),
                "{m}"
            );
        }
        assert!(matches!(
            resolve(&s, Ingress::Messages, "no-such-model", Selectors::default()),
            Resolved::Targets(_)
        ));
    }

    #[test]
    fn envelope_matches_node_shape() {
        let s = settings();
        let Resolved::Targets(t) = resolve(&s, Ingress::Messages, "no-such", Selectors::default())
        else {
            panic!()
        };
        let attempts: Vec<Attempt> = t
            .iter()
            .filter_map(|t| t.plan.as_ref().err().cloned())
            .collect();
        let (status, body) = envelope(&t, &attempts);
        assert_eq!(status, 400);
        assert_eq!(
            body["error"]["target_providers"],
            json!(["openai", "anthropic"])
        );
        assert_eq!(body["error"]["target_provider_names"], json!([]));
        assert_eq!(body["error"]["attempts"][1]["stage"], "model_resolution");
        assert!(body["error"]["attempts"][1].get("details").is_none());
    }
}
