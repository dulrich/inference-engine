//! Static gateway-key client auth, always on. One header, `x-gateway-key`, whose raw value is
//! compared whole against `AUTH_STATIC_API_KEYS`; no other header is consulted, so the
//! `authorization` OAuth bearer is never mistaken for the gateway key.

use axum::http::HeaderMap;

pub const HEADER: &str = "x-gateway-key";

/// `Bearer <token>` (case-insensitive scheme, whitespace-separated), trimmed. Used for the
/// OAuth passthrough, never for the gateway key.
pub fn bearer(value: &str) -> Option<&str> {
    let (scheme, rest) = value.split_at_checked(6)?;
    if !scheme.eq_ignore_ascii_case("bearer") || !rest.starts_with(char::is_whitespace) {
        return None;
    }
    Some(rest.trim()).filter(|t| !t.is_empty())
}

/// `Ok(())` or `(401, message)`.
pub fn check(keys: &[String], headers: &HeaderMap) -> Result<(), String> {
    match headers
        .get(HEADER)
        .and_then(|v| v.to_str().ok())
        .filter(|v| !v.is_empty())
    {
        None => Err(format!("Missing auth token header: {HEADER}")),
        Some(t) if keys.iter().any(|k| k == t) => Ok(()),
        Some(_) => Err("Invalid API key.".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hdrs(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        let mut h = HeaderMap::new();
        for (k, v) in pairs {
            h.insert(*k, v.parse().unwrap());
        }
        h
    }

    #[test]
    fn raw_x_gateway_key_only() {
        let keys = vec!["gk".to_string()];
        let missing = Err("Missing auth token header: x-gateway-key".to_string());
        let invalid = Err("Invalid API key.".to_string());
        assert_eq!(check(&keys, &hdrs(&[])), missing);
        assert_eq!(check(&keys, &hdrs(&[("x-gateway-key", "gk")])), Ok(()));
        assert_eq!(check(&keys, &hdrs(&[("x-gateway-key", "nope")])), invalid);
        // The raw value is compared whole: a Bearer form is not unwrapped.
        assert_eq!(
            check(&keys, &hdrs(&[("x-gateway-key", "Bearer gk")])),
            invalid
        );
        assert_eq!(
            check(
                &keys,
                &hdrs(&[("x-gateway-key", "gk"), ("authorization", "Bearer oauth")])
            ),
            Ok(())
        );
        // A valid key in any other header is not read.
        for (h, v) in [
            ("authorization", "Bearer gk"),
            ("authorization", "gk"),
            ("x-api-key", "gk"),
            ("api-key", "gk"),
            ("x-goog-api-key", "gk"),
            ("x-mcp-key", "gk"),
            ("x-codex-access-token", "gk"),
        ] {
            assert_eq!(check(&keys, &hdrs(&[(h, v)])), missing, "{h}");
        }
    }
}
