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

/// `Date.now()`.
pub fn now_ms() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_millis() as f64)
}

/// Days since 1970-01-01 of a proleptic-Gregorian civil date (Hinnant's days_from_civil);
/// an out-of-range day rolls over the way JSC's ISO parser lets `02-30` roll into March.
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// `Date.parse(s)` for the ISO-8601 family JSC accepts (measured against Bun 1.3 with
/// `TZ=UTC`, the container's zone): `YYYY`, `±YYYYYY`, `-M[M]`, `-D[D]`, then `T`/`t`/space,
/// `HH:mm[:ss[.fraction]]` (hour 24 allowed, second 60 not), `Z`/`z` or `±HH[:]mm`. A
/// date-time without an offset is local time, which is UTC in the image. Leading/trailing
/// whitespace, `,` fractions and JSC's non-ISO fallback forms ("Aug 24 2026") are NaN here
/// (None); no reader in this API is fed the fallback forms.
pub fn date_parse(s: &str) -> Option<f64> {
    let b = s.as_bytes();
    let mut i = 0;
    let digits = |i: &mut usize, min: usize, max: usize| -> Option<i64> {
        let start = *i;
        while *i < b.len() && *i - start < max && b[*i].is_ascii_digit() {
            *i += 1;
        }
        if *i - start < min {
            return None;
        }
        s[start..*i].parse().ok()
    };
    let year = match b.first()? {
        b'+' | b'-' => {
            let neg = b[0] == b'-';
            i = 1;
            let y = digits(&mut i, 6, 6)?;
            if neg {
                -y
            } else {
                y
            }
        }
        _ => digits(&mut i, 4, 4)?,
    };
    let (mut month, mut day) = (1, 1);
    if b.get(i) == Some(&b'-') {
        i += 1;
        month = digits(&mut i, 1, 2)?;
        if b.get(i) == Some(&b'-') {
            i += 1;
            day = digits(&mut i, 1, 2)?;
        }
    }
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    let mut ms = days_from_civil(year, month, day) * 86_400_000;
    if i == b.len() {
        return Some(ms as f64);
    }
    if !matches!(b[i], b'T' | b't' | b' ') {
        return None;
    }
    i += 1;
    let hour = digits(&mut i, 2, 2)?;
    if b.get(i) != Some(&b':') {
        return None;
    }
    i += 1;
    let minute = digits(&mut i, 2, 2)?;
    let mut second = 0;
    let mut millis = 0;
    if b.get(i) == Some(&b':') {
        i += 1;
        second = digits(&mut i, 2, 2)?;
        if b.get(i) == Some(&b'.') {
            i += 1;
            let start = i;
            while i < b.len() && b[i].is_ascii_digit() {
                i += 1;
            }
            if i == start {
                return None;
            }
            let frac = &s[start..i];
            let three: String = frac.chars().chain("000".chars()).take(3).collect();
            millis = three.parse::<i64>().ok()?;
        }
    }
    if hour > 24 || minute > 59 || second > 59 || (hour == 24 && (minute | second | millis) != 0) {
        return None;
    }
    ms += ((hour * 60 + minute) * 60 + second) * 1000 + millis;
    match b.get(i) {
        None => {}
        Some(b'Z' | b'z') if i + 1 == b.len() => {}
        Some(&sign @ (b'+' | b'-')) => {
            i += 1;
            let oh = digits(&mut i, 2, 2)?;
            if b.get(i) == Some(&b':') {
                i += 1;
            }
            let om = digits(&mut i, 2, 2)?;
            if i != b.len() || oh > 23 || om > 59 {
                return None;
            }
            let off = (oh * 60 + om) * 60_000;
            ms += if sign == b'+' { -off } else { off };
        }
        _ => return None,
    }
    Some(ms as f64)
}

/// Bun's `JSON.parse` error message (JSC), for the malformed-file texts a reader surfaces as
/// an entry's `error` (`routes/queue.ts` malformed queue/state files): a minimal JSON scan
/// that names the first failure the way JSC does. Only called after serde rejected `text`.
pub fn json_parse_error(text: &str) -> String {
    let mut p = JscScan {
        b: text.as_bytes(),
        i: 0,
    };
    let msg = match p.value() {
        Err(m) => m,
        Ok(()) => {
            p.ws();
            if p.i < p.b.len() {
                "Unable to parse JSON string".to_string()
            } else {
                "Unexpected EOF".to_string()
            }
        }
    };
    format!("JSON Parse error: {msg}")
}

struct JscScan<'a> {
    b: &'a [u8],
    i: usize,
}

impl JscScan<'_> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }
    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }
    fn string(&mut self) -> Result<(), String> {
        self.i += 1;
        while let Some(c) = self.peek() {
            self.i += 1;
            match c {
                b'"' => return Ok(()),
                b'\\' => self.i += 1,
                _ => {}
            }
        }
        Err("Unterminated string".into())
    }
    fn value(&mut self) -> Result<(), String> {
        self.ws();
        match self.peek() {
            None => Err("Unexpected EOF".into()),
            Some(b'{') => {
                self.i += 1;
                self.ws();
                match self.peek() {
                    Some(b'}') => {
                        self.i += 1;
                        return Ok(());
                    }
                    Some(b'"') => {}
                    _ => return Err("Expected '}'".into()),
                }
                loop {
                    self.string()?;
                    self.ws();
                    if self.peek() != Some(b':') {
                        return Err(
                            "Expected ':' before value in object property definition".into()
                        );
                    }
                    self.i += 1;
                    self.value()?;
                    self.ws();
                    match self.peek() {
                        Some(b',') => {
                            self.i += 1;
                            self.ws();
                            if self.peek() != Some(b'"') {
                                return Err("Property name must be a string literal".into());
                            }
                        }
                        Some(b'}') => {
                            self.i += 1;
                            return Ok(());
                        }
                        _ => return Err("Expected '}'".into()),
                    }
                }
            }
            Some(b'[') => {
                self.i += 1;
                self.ws();
                if self.peek() == Some(b']') {
                    self.i += 1;
                    return Ok(());
                }
                loop {
                    self.value()?;
                    self.ws();
                    match self.peek() {
                        Some(b',') => self.i += 1,
                        Some(b']') => {
                            self.i += 1;
                            return Ok(());
                        }
                        _ => return Err("Expected ']'".into()),
                    }
                }
            }
            Some(b'"') => self.string(),
            Some(c) if c.is_ascii_alphabetic() => {
                let start = self.i;
                while self.peek().is_some_and(|c| c.is_ascii_alphanumeric()) {
                    self.i += 1;
                }
                let word = String::from_utf8_lossy(&self.b[start..self.i]);
                if matches!(&*word, "true" | "false" | "null") {
                    Ok(())
                } else {
                    Err(format!("Unexpected identifier \"{word}\""))
                }
            }
            Some(c) if c == b'-' || c.is_ascii_digit() => {
                while self.peek().is_some_and(|c| {
                    c.is_ascii_digit() || matches!(c, b'-' | b'+' | b'.' | b'e' | b'E')
                }) {
                    self.i += 1;
                }
                Ok(())
            }
            Some(_) => Err("Unable to parse JSON string".into()),
        }
    }
}

/// `JSON.parse(text)` with the JSC error text on failure.
pub fn json_parse_js(text: &str) -> Result<Value, String> {
    json_parse(text).map_err(|_| json_parse_error(text))
}

/// `Object.entries(obj)` order: array-index keys ascending, then the rest in insertion order.
pub fn js_entries(map: &serde_json::Map<String, Value>) -> Vec<(&String, &Value)> {
    let mut idx: Vec<(&String, &Value)> = map
        .iter()
        .filter(|(k, _)| bd_domain::jsout::is_array_index(k))
        .collect();
    idx.sort_by_key(|(k, _)| k.parse::<u64>().unwrap_or(0));
    idx.extend(
        map.iter()
            .filter(|(k, _)| !bd_domain::jsout::is_array_index(k)),
    );
    idx
}

/// `n` random bytes from the kernel (`crypto.randomUUID`'s entropy source).
pub fn random_bytes<const N: usize>() -> std::io::Result<[u8; N]> {
    use std::io::Read;
    let mut buf = [0u8; N];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut buf)?;
    Ok(buf)
}

/// `crypto.randomUUID()`: a v4 UUID, lowercase hex.
pub fn random_uuid() -> std::io::Result<String> {
    let mut b = random_bytes::<16>()?;
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h: String = b.iter().map(|x| format!("{x:02x}")).collect();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    ))
}

/// `String(n)` for a JS number.
pub fn num_str(n: f64) -> String {
    bd_domain::jsout::number_to_string(n).unwrap_or_else(|| "NaN".into())
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

    /// Values measured with `TZ=UTC bun -e 'Date.parse(s)'` (Bun 1.3).
    #[test]
    fn date_parse_matches_jsc_on_iso_forms() {
        let cases: &[(&str, Option<f64>)] = &[
            (
                "2026-08-24T20:00:00.000000+00:00",
                Some(1_787_601_600_000.0),
            ),
            ("2026-08-24", Some(1_787_529_600_000.0)),
            ("2026-08-24T20:00", Some(1_787_601_600_000.0)),
            ("2026-08-24T20:00:00", Some(1_787_601_600_000.0)),
            ("2026-08-24 20:00:00Z", Some(1_787_601_600_000.0)),
            ("2026-8-24", Some(1_787_529_600_000.0)),
            ("2026-08-24T24:00:00Z", Some(1_787_616_000_000.0)),
            ("2026-02-30T00:00:00Z", Some(1_772_409_600_000.0)),
            ("+002026-08-24T00:00:00Z", Some(1_787_529_600_000.0)),
            ("2026-08-24T20:00:00.1Z", Some(1_787_601_600_100.0)),
            ("2026-08-24T20:00Z", Some(1_787_601_600_000.0)),
            ("2026", Some(1_767_225_600_000.0)),
            ("2026-08", Some(1_785_542_400_000.0)),
            ("2026-08-24t20:00:00z", Some(1_787_601_600_000.0)),
            ("2026-08-24T20:00:00+0100", Some(1_787_598_000_000.0)),
            ("not a date", None),
            ("yesterday-ish", None),
            ("not-a-date", None),
            ("", None),
            (" ", None),
            (" 2026-08-24T20:00:00Z", None),
            ("2026-08-24T20:00:00Z ", None),
            ("2026-08-24T20:00:00,5Z", None),
            ("2026-08-24T20:00:60Z", None),
            ("13:00", None),
        ];
        for (s, want) in cases {
            assert_eq!(date_parse(s), *want, "{s:?}");
        }
    }

    /// Messages measured with `bun -e 'JSON.parse(s)'` (Bun 1.3).
    #[test]
    fn json_parse_error_matches_jsc() {
        let cases = [
            ("{ \"queue_format\": 1, \"cells\": [\n", "Unexpected EOF"),
            (
                "{ \"queue_state\": \"running\", \"cells\": {\n",
                "Expected '}'",
            ),
            ("{", "Expected '}'"),
            ("", "Unexpected EOF"),
            ("not json", "Unexpected identifier \"not\""),
            ("[1,", "Unexpected EOF"),
            ("{\"a\":1}x", "Unable to parse JSON string"),
            (
                "{\"a\" 1}",
                "Expected ':' before value in object property definition",
            ),
            ("\"abc", "Unterminated string"),
            ("{,}", "Expected '}'"),
            ("{\"a\":1,", "Property name must be a string literal"),
            ("{\"a\":1", "Expected '}'"),
            ("[1", "Expected ']'"),
        ];
        for (text, want) in cases {
            assert_eq!(
                json_parse_error(text),
                format!("JSON Parse error: {want}"),
                "{text:?}"
            );
        }
    }

    #[test]
    fn js_entry_order_and_uuid() {
        let m: serde_json::Map<String, Value> =
            serde_json::from_str(r#"{"b":1,"10":2,"a":3,"2":4}"#).unwrap();
        let keys: Vec<&str> = js_entries(&m)
            .into_iter()
            .map(|(k, _)| k.as_str())
            .collect();
        assert_eq!(keys, ["2", "10", "b", "a"]);
        let u = random_uuid().unwrap();
        assert_eq!((u.len(), &u[14..15]), (36, "4"));
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
