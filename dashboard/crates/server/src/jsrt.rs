//! Runtime-side JS semantics the Bun API leans on implicitly: `JSON.parse` number handling,
//! `localeCompare`, default `Array#sort` (UTF-16 code units), `node:path` `join`/`basename`,
//! `new Date().toISOString()`, and Hono's URI decoding (`decodeURI` with `%25` protection,
//! per-run `tryDecode` fallback).
use icu_collator::options::CollatorOptions;
use icu_collator::{Collator, CollatorBorrowed};
use serde_json::{Number, Value};
use std::cmp::Ordering;
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

/// `JSON.parse(text)`: serde_json with every number read as a JS `Number` (an integer beyond
/// 2^53 becomes the nearest f64, exactly as V8/JSC would hold it).
pub fn json_parse(text: &str) -> Result<Value, serde_json::Error> {
    let mut v: Value = serde_json::from_str(text)?;
    to_js_numbers(&mut v);
    Ok(v)
}

fn to_js_numbers(v: &mut Value) {
    match v {
        Value::Number(n) => {
            let wide = n.as_i64().map_or_else(
                || n.as_u64().is_some(),
                |i| i.unsigned_abs() > bd_domain::js::MAX_SAFE_INTEGER as u64,
            );
            if wide {
                if let Some(f) = n.as_f64().and_then(Number::from_f64) {
                    *n = f;
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(to_js_numbers),
        Value::Object(map) => map.values_mut().for_each(to_js_numbers),
        _ => {}
    }
}

/// A JSON value as a JS `number`, when it is one (`typeof v === "number"`; finite by
/// construction after [`json_parse`]).
pub fn as_number(v: Option<&Value>) -> Option<f64> {
    v.and_then(Value::as_f64).filter(|f| f.is_finite())
}

/// `JSON.stringify(value)`.
pub fn stringify(v: &Value) -> String {
    bd_domain::jsout::stringify(v)
}

fn collator() -> &'static CollatorBorrowed<'static> {
    static C: OnceLock<CollatorBorrowed<'static>> = OnceLock::new();
    C.get_or_init(|| {
        Collator::try_new(Default::default(), CollatorOptions::default())
            .expect("compiled root collation data")
    })
}

/// `a.localeCompare(b)` (root locale, default options — plan Spike 2).
pub fn locale_compare(a: &str, b: &str) -> Ordering {
    collator().compare(a, b)
}

/// Default `Array.prototype.sort()` order on strings: UTF-16 code units.
pub fn utf16_cmp(a: &str, b: &str) -> Ordering {
    a.encode_utf16().cmp(b.encode_utf16())
}

/// `path.normalize` (POSIX) of a non-empty path.
fn normalize(path: &str) -> String {
    let absolute = path.starts_with('/');
    let mut parts: Vec<&str> = Vec::new();
    for seg in path.split('/') {
        match seg {
            "" | "." => {}
            ".." => {
                if parts.last().is_some_and(|p| *p != "..") {
                    parts.pop();
                } else if !absolute {
                    parts.push("..");
                }
            }
            s => parts.push(s),
        }
    }
    let body = parts.join("/");
    let trailing = path.ends_with('/') && !body.is_empty();
    let mut out = match (absolute, body.is_empty()) {
        (true, _) => format!("/{body}"),
        (false, true) => ".".to_string(),
        (false, false) => body,
    };
    if trailing {
        out.push('/');
    }
    out
}

/// `path.join(a, b)` (POSIX).
pub fn path_join(a: &str, b: &str) -> String {
    let joined = match (a.is_empty(), b.is_empty()) {
        (true, true) => return ".".into(),
        (true, false) => b.to_string(),
        (false, true) => a.to_string(),
        (false, false) => format!("{a}/{b}"),
    };
    normalize(&joined)
}

/// `path.basename(p)` (POSIX, no suffix).
pub fn basename(p: &str) -> &str {
    p.trim_end_matches('/').rsplit('/').next().unwrap_or("")
}

/// `new Date().toISOString()`.
pub fn iso_now() -> String {
    let ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as i64);
    iso_from_ms(ms)
}

/// `new Date(ms).toISOString()` for years 0..=9999.
pub fn iso_from_ms(ms: i64) -> String {
    let days = ms.div_euclid(86_400_000);
    let rem = ms.rem_euclid(86_400_000);
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{:03}Z",
        rem / 3_600_000,
        rem / 60_000 % 60,
        rem / 1000 % 60,
        rem % 1000
    )
}

/// One `%XX`-escaped run decoded as UTF-8, or None (`URIError`).
fn decode_escapes(s: &str, keep_reserved: bool) -> Option<String> {
    let b = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] != b'%' {
            let ch = s[i..].chars().next()?;
            out.push(ch);
            i += ch.len_utf8();
            continue;
        }
        let byte = |at: usize| -> Option<u8> {
            let h = b.get(at + 1..at + 3)?;
            if !h.iter().all(u8::is_ascii_hexdigit) || b[at] != b'%' {
                return None;
            }
            u8::from_str_radix(std::str::from_utf8(h).ok()?, 16).ok()
        };
        let first = byte(i)?;
        let len = match first {
            0x00..=0x7F => 1,
            0xC2..=0xDF => 2,
            0xE0..=0xEF => 3,
            0xF0..=0xF4 => 4,
            _ => return None,
        };
        let mut bytes = vec![first];
        for k in 1..len {
            bytes.push(byte(i + 3 * k)?);
        }
        let decoded = std::str::from_utf8(&bytes).ok()?;
        // decodeURI keeps the escape of a reserved character (`;/?:@&=+$,#`).
        if keep_reserved && len == 1 && ";/?:@&=+$,#".contains(first as char) {
            out.push_str(&s[i..i + 3]);
        } else {
            out.push_str(decoded);
        }
        i += 3 * len;
    }
    Some(out)
}

/// Hono's `tryDecode(str, decoder)`: the whole string, else each `(?:%[0-9A-Fa-f]{2})+` run
/// on its own, an undecodable run kept as written.
fn try_decode(s: &str, keep_reserved: bool) -> String {
    if let Some(d) = decode_escapes(s, keep_reserved) {
        return d;
    }
    let b = s.as_bytes();
    let is_escape = |i: usize| {
        b.get(i) == Some(&b'%')
            && b.get(i + 1..i + 3)
                .is_some_and(|h| h.iter().all(u8::is_ascii_hexdigit))
    };
    let mut out = String::new();
    let mut i = 0;
    while i < b.len() {
        if is_escape(i) {
            let start = i;
            while is_escape(i) {
                i += 3;
            }
            let run = &s[start..i];
            out.push_str(&decode_escapes(run, keep_reserved).unwrap_or_else(|| run.to_string()));
        } else {
            let ch = s[i..].chars().next().unwrap_or_default();
            out.push(ch);
            i += ch.len_utf8();
        }
    }
    out
}

/// `decodeURIComponent(s)`, None where it throws.
pub fn decode_uri_component(s: &str) -> Option<String> {
    decode_escapes(s, false)
}

/// Hono's `getPath` decode of a raw request path: `%25` protected, then `tryDecodeURI`.
pub fn hono_path(raw: &str) -> String {
    if !raw.contains('%') {
        return raw.to_string();
    }
    let protected = raw.replace("%25", "%2525");
    try_decode(&protected, true)
}

/// Hono's `tryDecodeURIComponent` (route params).
pub fn hono_param(raw: &str) -> String {
    if raw.contains('%') {
        try_decode(raw, false)
    } else {
        raw.to_string()
    }
}

/// Hono's `_decodeURI` (query keys and values): `+` -> space, then `tryDecodeURIComponent`.
pub fn hono_query_decode(raw: &str) -> String {
    hono_param(&raw.replace('+', " "))
}

/// `c.req.queries(name)`: every value of `name` in the raw query, decoded; None when absent.
pub fn query_all(raw: Option<&str>, name: &str) -> Option<Vec<String>> {
    let mut out = Vec::new();
    for pair in raw?.split('&') {
        let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
        if !k.is_empty() && hono_query_decode(k) == name {
            out.push(hono_query_decode(v));
        }
    }
    (!out.is_empty()).then_some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn node_path_join() {
        assert_eq!(path_join("/records", "a"), "/records/a");
        assert_eq!(path_join("/records/", "a"), "/records/a");
        assert_eq!(path_join("records", "a"), "records/a");
        assert_eq!(path_join("./r/../s", "a"), "s/a");
        assert_eq!(path_join("../r", "a"), "../r/a");
        assert_eq!(path_join("", "a"), "a");
        assert_eq!(path_join("//x//", "a"), "/x/a");
        assert_eq!(basename("/records/b-1"), "b-1");
    }

    #[test]
    fn iso_dates() {
        assert_eq!(iso_from_ms(0), "1970-01-01T00:00:00.000Z");
        assert_eq!(iso_from_ms(1_709_210_096_789), "2024-02-29T12:34:56.789Z");
        assert_eq!(iso_from_ms(951_782_400_000), "2000-02-29T00:00:00.000Z");
    }

    #[test]
    fn json_parse_reads_js_numbers() {
        let v = json_parse(r#"{"a":12345678901234567890,"b":-9007199254740993,"c":7,"d":1.0}"#)
            .unwrap();
        assert_eq!(
            stringify(&v),
            r#"{"a":12345678901234567000,"b":-9007199254740992,"c":7,"d":1}"#
        );
        assert_eq!(json!(1), json_parse("1").unwrap());
    }

    #[test]
    fn hono_decoding() {
        assert_eq!(hono_path("/api/ladders/a%2Fb"), "/api/ladders/a%2Fb");
        assert_eq!(hono_path("/api/ladders/a%252Fb"), "/api/ladders/a%252Fb");
        assert_eq!(hono_path("/api/ladders/a/b%20c"), "/api/ladders/a/b c");
        assert_eq!(hono_path("/x/%E0%A4%A"), "/x/%E0%A4%A");
        assert_eq!(decode_uri_component("a%2Fb").as_deref(), Some("a/b"));
        assert_eq!(decode_uri_component("%E0%A4%A"), None);
        assert_eq!(hono_param("a+b%20c"), "a+b c");
        assert_eq!(
            query_all(Some("id=a&x=1&id=b+c&id=%41"), "id"),
            Some(vec!["a".into(), "b c".into(), "A".into()])
        );
        assert_eq!(query_all(Some("x=1"), "id"), None);
        assert_eq!(query_all(Some("id="), "id"), Some(vec![String::new()]));
    }

    /// `db/experiments.test.ts` modelKey `localeCompare` ordering beyond the fixture keys:
    /// case-insensitive-first, punctuation before digits before letters, `-`/`_`/`.` ignorable
    /// differences resolved after letters.
    #[test]
    fn locale_compare_matches_icu_root() {
        let mut keys = vec![
            "qwen3.6-35b",
            "Qwen3-4B",
            "gemma-4-12b-qat",
            "bge_reranker",
            "bge-m3",
            "Gemma-4-12b",
            "a",
            "B",
            "_x",
            "10",
            "9",
        ];
        keys.sort_by(|a, b| locale_compare(a, b));
        assert_eq!(
            keys,
            vec![
                "_x",
                "10",
                "9",
                "a",
                "B",
                "bge_reranker",
                "bge-m3",
                "Gemma-4-12b",
                "gemma-4-12b-qat",
                "Qwen3-4B",
                "qwen3.6-35b",
            ]
        );
        assert_eq!(locale_compare("a", "A"), Ordering::Less);
        assert_eq!(locale_compare("x", "x"), Ordering::Equal);
    }
}
