//! Model → ordered targets: an explicit `provider/model` prefix, else the `default_routes`,
//! each validated against the provider's model list, then filtered to the providers that
//! speak the ingress protocol (a request never crosses protocols).

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
    /// `openai_chat_completions` provider with the native-messages selector, on Messages or
    /// CountTokens ingress.
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

/// One failed attempt, rendered into the `All target providers failed.` envelope.
#[derive(Clone, Debug)]
pub struct Attempt {
    pub provider: Arc<Provider>,
    pub stage: &'static str,
    pub message: String,
    pub status: Option<u16>,
    pub details: Option<Value>,
}

/// One target in the ordered route list: either ready to call or already failed (model
/// validation) — a failed one still counts as "a next target exists".
pub struct Target {
    pub provider: Arc<Provider>,
    /// Named explicitly by a model prefix vs a default route; only explicit ones are listed
    /// in `target_provider_names`.
    pub explicit: bool,
    pub plan: Result<Candidate, Attempt>,
}

pub enum Resolved {
    /// Targets in fallthrough order, already filtered to the ingress protocol.
    Targets(Vec<Target>),
    /// The model resolves only to providers that do not speak this ingress protocol.
    Mismatch { model: String, provider: String },
}

fn leg_for(p: &Provider, ingress: Ingress) -> Option<Leg> {
    match (&p.kind, ingress) {
        (Kind::Anthropic, Ingress::Messages | Ingress::CountTokens) => Some(Leg::Anthropic),
        (Kind::OpenAiChat, Ingress::Chat) => Some(Leg::OpenAiChat),
        (Kind::OpenAiChat, _) if p.native_messages => Some(Leg::LocalMessages),
        _ => None,
    }
}

/// Provider by name, case-insensitive (names only; there are no `openai`/`anthropic` type
/// routes).
fn route_by(s: &Settings, sel: &str) -> Option<Arc<Provider>> {
    let sel = sel.trim();
    s.providers
        .iter()
        .find(|p| !sel.is_empty() && p.name.eq_ignore_ascii_case(sel))
        .cloned()
}

/// Split a model reference into `(provider prefix, model)`; a prefix naming no provider leaves
/// the whole string as a bare model.
fn model_ref(s: &Settings, raw: &str) -> (Option<Arc<Provider>>, String) {
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

fn validates(p: &Provider, model: &str) -> bool {
    p.models.is_empty() || p.models.iter().any(|m| m == model)
}

pub fn resolve(s: &Settings, ingress: Ingress, requested: &str) -> Resolved {
    let requested = requested.trim();
    let (mref, model) = model_ref(s, requested);
    let routes = match mref {
        Some(p) => vec![(p, true)],
        None => default_routes(s),
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
        let plan = if validates(&p, &model) {
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

/// The `All target providers failed.` fallback payload; absent fields are omitted, never
/// serialized as `null`.
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
        let env = |k: &str| (k == "AUTH_STATIC_API_KEYS").then(|| "gk".to_string());
        from_json(&serde_json::from_str(text).unwrap(), &env).unwrap()
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
        let n = |i, m| ok(&resolve(&s, i, m));
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
        // A prefix naming no provider (incl. the removed `openai`/`anthropic` type routes) is
        // part of a bare model id.
        assert!(n(Ingress::Messages, "nope/claude-opus-5-5").is_empty());
        assert!(n(Ingress::Chat, "openai/qwen36-a3b").is_empty());
        assert_eq!(
            n(Ingress::Messages, "codex-b/gpt-6-luna"),
            own("codex-b", "gpt-6-luna", Leg::Anthropic)
        );
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
                matches!(resolve(&s, i, m), Resolved::Mismatch { .. }),
                "{m}"
            );
        }
        assert!(matches!(
            resolve(&s, Ingress::Messages, "no-such-model"),
            Resolved::Targets(_)
        ));
    }

    #[test]
    fn envelope_matches_node_shape() {
        let s = settings();
        let Resolved::Targets(t) = resolve(&s, Ingress::Messages, "no-such") else {
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
