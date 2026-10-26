//! Native request decoding (Decision 5): query strings, path segments and SPA paths.
//!
//! Hand-written rather than `percent-encoding`: that crate's `percent_decode_str` passes a
//! malformed escape (`%zz`) through unchanged, and the fleet rule is malformed → 400.

use std::fmt;
use std::path::{Component, Path};

/// A malformed percent escape or a decoded value that is not UTF-8 (respond 400).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DecodeError;

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("malformed percent-encoding")
    }
}

impl std::error::Error for DecodeError {}

fn hex(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

/// Decodes `%XX` escapes once (both bytes must be hex digits); `plus` maps `+` to a space.
fn decode(s: &str, plus: bool) -> Result<String, DecodeError> {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' => {
                let hi = bytes.get(i + 1).copied().and_then(hex).ok_or(DecodeError)?;
                let lo = bytes.get(i + 2).copied().and_then(hex).ok_or(DecodeError)?;
                out.push(hi * 16 + lo);
                i += 3;
            }
            b'+' if plus => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8(out).map_err(|_| DecodeError)
}

/// Decoded query pairs in order; duplicates are kept, [`Query::get`] returns the first.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Query(pub Vec<(String, String)>);

impl Query {
    /// The first value for `key` (first wins for a duplicate key).
    pub fn get(&self, key: &str) -> Option<&str> {
        get(&self.0, key)
    }
}

/// The first value for `key` in decoded `pairs`.
pub fn get<'a>(pairs: &'a [(String, String)], key: &str) -> Option<&'a str> {
    pairs
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.as_str())
}

/// Parses a raw query string (no leading `?`): split on `&`, then on the first `=`, `+` →
/// space, each key and value percent-decoded once. Empty parts are skipped; a key without
/// `=` has the empty value. Any malformed escape → `Err`.
pub fn parse_query(raw: &str) -> Result<Vec<(String, String)>, DecodeError> {
    raw.split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            Ok((decode(k, true)?, decode(v, true)?))
        })
        .collect()
}

/// A path segment decoded once (`%2525` → `%25`, `+` kept); malformed or non-UTF-8 → `Err`.
pub fn decode_path_segment(s: &str) -> Result<String, DecodeError> {
    decode(s, false)
}

/// An SPA request path decoded once, as a relative path: `Ok(Some(rel))` when every
/// component is a plain `Normal` name, `Ok(None)` for an empty path or any `.`/`..`/root
/// component (not a file candidate), `Err` when the path is malformed.
pub fn decode_path(s: &str) -> Result<Option<String>, DecodeError> {
    let decoded = decode(s, false)?;
    let rel = decoded.trim_start_matches('/');
    let mut comps = Path::new(rel).components().peekable();
    if comps.peek().is_none() || !comps.all(|c| matches!(c, Component::Normal(_))) {
        return Ok(None);
    }
    Ok(Some(rel.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_rule() {
        let q = Query(parse_query("a=1+2&b=%2525&a=3&flag").unwrap());
        assert_eq!(q.get("a"), Some("1 2"));
        assert_eq!(q.get("b"), Some("%25"));
        assert_eq!(q.get("flag"), Some(""));
        assert_eq!(q.get("x"), None);
        assert_eq!(parse_query("a%20b=c").unwrap()[0].0, "a b");
        for bad in ["a=%zz", "a=%+1", "%1=x", "a=%ff"] {
            assert_eq!(parse_query(bad), Err(DecodeError), "{bad}");
        }
    }

    #[test]
    fn path_segments() {
        assert_eq!(decode_path_segment("a%2525b").unwrap(), "a%25b");
        assert_eq!(decode_path_segment("a+b%2F").unwrap(), "a+b/");
        assert_eq!(decode_path_segment("%"), Err(DecodeError));
        assert_eq!(decode_path_segment("%C3"), Err(DecodeError));
    }

    #[test]
    fn spa_paths() {
        assert_eq!(
            decode_path("/assets/a%20b.js").unwrap().as_deref(),
            Some("assets/a b.js")
        );
        assert_eq!(decode_path("/").unwrap(), None);
        assert_eq!(decode_path("/a/%2e%2e/b").unwrap(), None);
        assert_eq!(decode_path("/./x").unwrap(), None);
        assert_eq!(decode_path("/%zz"), Err(DecodeError));
    }
}
