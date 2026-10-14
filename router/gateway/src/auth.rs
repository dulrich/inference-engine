//! Static gateway-key client auth (Node `src/gateway/auth.ts:161-191`, `resolveAuthToken` `:614`,
//! `normalizeAuthTokenValue` `:661`).

use crate::config::AuthConfig;
use axum::http::HeaderMap;

const SDK_TOKEN_HEADERS: [&str; 6] = [
    "authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "x-mcp-key",
    "x-codex-access-token",
];

/// `Bearer <token>` (case-insensitive scheme, whitespace-separated), trimmed.
pub fn bearer(value: &str) -> Option<&str> {
    let (scheme, rest) = value.split_at_checked(6)?;
    if !scheme.eq_ignore_ascii_case("bearer") || !rest.starts_with(char::is_whitespace) {
        return None;
    }
    Some(rest.trim()).filter(|t| !t.is_empty())
}

fn normalize<'a>(raw: &'a str, header: &str, primary: &str, bearer_only: bool) -> Option<&'a str> {
    if header == "authorization" {
        return bearer(raw).or((header == primary && !bearer_only).then_some(raw));
    }
    if header == primary {
        return if bearer_only { bearer(raw) } else { Some(raw) };
    }
    Some(raw)
}

/// `Ok(())` or `(401, message)`.
pub fn check(cfg: &AuthConfig, headers: &HeaderMap) -> Result<(), String> {
    if !cfg.enabled {
        return Ok(());
    }
    let mut candidates = vec![cfg.header.as_str()];
    candidates.extend(SDK_TOKEN_HEADERS.iter().filter(|h| **h != cfg.header));
    let mut invalid_format = false;
    let mut token = None;
    for name in candidates {
        let Some(raw) = headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .filter(|v| !v.is_empty())
        else {
            continue;
        };
        if let Some(t) = normalize(raw, name, &cfg.header, cfg.bearer_only) {
            token = Some(t);
            break;
        }
        invalid_format = true;
    }
    match token {
        None if !cfg.required => Ok(()),
        None if invalid_format => Err("Invalid auth token format.".into()),
        None => Err(format!("Missing auth token header: {}", cfg.header)),
        Some(t) if cfg.keys.iter().any(|k| k == t) => Ok(()),
        Some(_) => Err("Invalid API key.".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(bearer_only: bool) -> AuthConfig {
        AuthConfig {
            enabled: true,
            required: true,
            keys: vec!["gk".into()],
            header: "x-gateway-key".into(),
            bearer_only,
        }
    }

    fn hdrs(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        let mut h = HeaderMap::new();
        for (k, v) in pairs {
            h.insert(*k, v.parse().unwrap());
        }
        h
    }

    #[test]
    fn auth_matrix() {
        let c = cfg(false);
        assert_eq!(
            check(&c, &hdrs(&[])),
            Err("Missing auth token header: x-gateway-key".into())
        );
        assert_eq!(check(&c, &hdrs(&[("x-gateway-key", "gk")])), Ok(()));
        assert_eq!(
            check(&c, &hdrs(&[("x-gateway-key", "nope")])),
            Err("Invalid API key.".into())
        );
        // Node with bearerOnly=false takes the primary header's raw value, so a Bearer-form
        // x-gateway-key is compared verbatim and rejected (auth.ts:682-685).
        assert_eq!(
            check(&c, &hdrs(&[("x-gateway-key", "Bearer gk")])),
            Err("Invalid API key.".into())
        );
        // Primary header wins over the OAuth bearer, which is otherwise the next candidate.
        assert_eq!(
            check(
                &c,
                &hdrs(&[("x-gateway-key", "gk"), ("authorization", "Bearer oauth")])
            ),
            Ok(())
        );
        assert_eq!(
            check(&c, &hdrs(&[("authorization", "Bearer oauth")])),
            Err("Invalid API key.".into())
        );
        assert_eq!(check(&c, &hdrs(&[("authorization", "Bearer gk")])), Ok(()));
        assert_eq!(
            check(&c, &hdrs(&[("authorization", "Basic x")])),
            Err("Invalid auth token format.".into())
        );
        let strict = cfg(true);
        assert_eq!(
            check(&strict, &hdrs(&[("x-gateway-key", "Bearer gk")])),
            Ok(())
        );
        assert_eq!(
            check(&strict, &hdrs(&[("x-gateway-key", "gk")])),
            Err("Invalid auth token format.".into())
        );
        let off = AuthConfig {
            enabled: false,
            ..cfg(false)
        };
        assert_eq!(check(&off, &hdrs(&[])), Ok(()));
    }
}
