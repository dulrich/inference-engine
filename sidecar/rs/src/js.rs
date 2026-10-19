//! JavaScript semantics the Node sidecar relies on implicitly, made explicit for parity.
//!
//! Every wire body here is a `serde_json::Value` (`preserve_order`), and these helpers answer
//! the questions JS answers silently: `Number(str)`, truthiness, `String(v)`, `.length` in
//! UTF-16 code units, `.trim()`'s whitespace set, and `JSON.stringify` byte output (JS property
//! order — array-index keys first, ascending — and JS number formatting).

use serde_json::{Map, Value};
use std::cmp::Ordering;

/// ECMAScript WhiteSpace + LineTerminator (the `\s` class and `String.prototype.trim` set).
pub fn is_js_space(c: char) -> bool {
    matches!(
        c,
        '\t' | '\n' | '\u{0B}' | '\u{0C}' | '\r' | ' ' | '\u{A0}' | '\u{1680}' | '\u{2000}'
            ..='\u{200A}'
                | '\u{2028}'
                | '\u{2029}'
                | '\u{202F}'
                | '\u{205F}'
                | '\u{3000}'
                | '\u{FEFF}'
    )
}

/// The characters a regex `.` does not match and `^`/`$` (with `m`) break on.
pub fn is_line_terminator(c: char) -> bool {
    matches!(c, '\n' | '\r' | '\u{2028}' | '\u{2029}')
}

pub fn trim(s: &str) -> &str {
    s.trim_matches(is_js_space)
}

pub fn trim_start(s: &str) -> &str {
    s.trim_start_matches(is_js_space)
}

/// `str.length` — UTF-16 code units.
pub fn utf16_len(s: &str) -> usize {
    s.encode_utf16().count()
}

/// `str.slice(0, n)` in UTF-16 code units. Deviation: a surrogate pair straddling the cut is
/// dropped whole (JS would keep a lone high surrogate, which a Rust `String` cannot hold).
pub fn utf16_prefix(s: &str, n: usize) -> &str {
    let mut units = 0;
    for (i, c) in s.char_indices() {
        units += c.len_utf16();
        if units > n {
            return &s[..i];
        }
    }
    s
}

/// Compare two strings the way `Array.prototype.sort` does by default (UTF-16 code units).
pub fn cmp_utf16(a: &str, b: &str) -> Ordering {
    a.encode_utf16().cmp(b.encode_utf16())
}

/// JS `Number(string)`: trims JS whitespace, `""` -> 0, decimal literals with optional sign,
/// fraction and exponent, unsigned `0x`/`0o`/`0b` integers, `[+-]Infinity`; anything else NaN.
pub fn js_number(raw: &str) -> f64 {
    let s = trim(raw);
    if s.is_empty() {
        return 0.0;
    }
    let lower = s.as_bytes();
    if lower.len() > 2 && lower[0] == b'0' {
        let radix = match lower[1] {
            b'x' | b'X' => 16,
            b'o' | b'O' => 8,
            b'b' | b'B' => 2,
            _ => 0,
        };
        if radix != 0 {
            let digits = &s[2..];
            let mut acc = 0f64;
            for c in digits.chars() {
                match c.to_digit(radix) {
                    Some(d) => acc = acc * f64::from(radix) + f64::from(d),
                    None => return f64::NAN,
                }
            }
            return acc;
        }
    }
    let (sign, body) = match s.as_bytes()[0] {
        b'+' => (1.0, &s[1..]),
        b'-' => (-1.0, &s[1..]),
        _ => (1.0, s),
    };
    if body == "Infinity" {
        return sign * f64::INFINITY;
    }
    // StrUnsignedDecimalLiteral: (digits [. digits?] | . digits) ([eE] [+-]? digits)?
    let b = body.as_bytes();
    let mut i = 0;
    let int_start = i;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    let int_digits = i - int_start;
    let mut frac_digits = 0;
    if i < b.len() && b[i] == b'.' {
        i += 1;
        let f = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        frac_digits = i - f;
    }
    if int_digits == 0 && frac_digits == 0 {
        return f64::NAN;
    }
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            i += 1;
        }
        let e = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == e {
            return f64::NAN;
        }
    }
    if i != b.len() {
        return f64::NAN;
    }
    match body.parse::<f64>() {
        Ok(v) => sign * v,
        Err(_) => f64::NAN,
    }
}

/// JS truthiness of a JSON value (`None` = `undefined`).
pub fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0 && !f.is_nan()),
        Some(Value::String(s)) => !s.is_empty(),
        Some(_) => true,
    }
}

/// `a ?? b` reading: a present, non-null value.
pub fn present(v: Option<&Value>) -> Option<&Value> {
    v.filter(|x| !x.is_null())
}

/// `obj?.key` on an arbitrary value: only objects have keys here (array/string props aside).
pub fn prop<'a>(v: Option<&'a Value>, key: &str) -> Option<&'a Value> {
    v.and_then(|x| x.as_object()).and_then(|m| m.get(key))
}

/// JS `Number.prototype.toString()` / `JSON.stringify` number text.
pub fn fmt_number(f: f64) -> String {
    if f.is_nan() {
        return "NaN".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    if f == 0.0 {
        return "0".into();
    }
    let a = f.abs();
    if !(1e-6..1e21).contains(&a) {
        let s = format!("{f:e}");
        return match s.split_once('e') {
            Some((m, e)) if !e.starts_with('-') => format!("{m}e+{e}"),
            _ => s,
        };
    }
    if f.fract() == 0.0 && a >= 9.007_199_254_740_992e15 {
        return big_integer_text(f);
    }
    format!("{f}")
}

/// Integral magnitudes in [2^53, 1e21): JS prints the shortest round-tripping digit string,
/// choosing the candidate closest to the exact value when several tie on length (Number::
/// toString step 5); Rust's shortest form may pick a neighbour (measured: 123456789012345680000
/// in Node vs ...670000 from `{}`). Exact value fits `u128`, so compare candidates exactly.
fn big_integer_text(f: f64) -> String {
    let exact = f.abs() as u128;
    let sci = format!("{:e}", f.abs());
    let (mant, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let digits: String = mant.chars().filter(char::is_ascii_digit).collect();
    let k = digits.len() as u32;
    let n: u32 = exp.parse::<u32>().unwrap_or(0) + 1;
    let scale = 10u128.pow(n.saturating_sub(k));
    let base: u128 = digits.parse().unwrap_or(0);
    let best = [base.saturating_sub(1), base, base + 1]
        .into_iter()
        .filter(|c| (*c * scale) as f64 == f.abs())
        .min_by_key(|c| (c * scale).abs_diff(exact))
        .unwrap_or(base);
    let sign = if f < 0.0 { "-" } else { "" };
    format!("{sign}{}", best * scale)
}

/// A JS number as a JSON value: integral values become integer `Number`s (so plain
/// `serde_json` output matches JS too), non-finite becomes `null` (as `JSON.stringify` does).
pub fn num(f: f64) -> Value {
    if !f.is_finite() {
        return Value::Null;
    }
    if f.fract() == 0.0 && f.abs() < 9.007_199_254_740_992e15 {
        return Value::from(f as i64);
    }
    serde_json::Number::from_f64(f).map_or(Value::Null, Value::Number)
}

/// JS `ToNumber` of a JSON value (`None` = `undefined` -> NaN).
pub fn to_number(v: Option<&Value>) -> f64 {
    match v {
        None => f64::NAN,
        Some(Value::Null) => 0.0,
        Some(Value::Bool(b)) => f64::from(u8::from(*b)),
        Some(Value::Number(n)) => n.as_f64().unwrap_or(f64::NAN),
        Some(Value::String(s)) => js_number(s),
        Some(Value::Array(a)) => match a.len() {
            0 => 0.0,
            1 => js_number(&js_string(&a[0])),
            _ => f64::NAN,
        },
        Some(Value::Object(_)) => f64::NAN,
    }
}

/// JS `String(v)`.
pub fn js_string(v: &Value) -> String {
    match v {
        Value::Null => "null".into(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => fmt_number(n.as_f64().unwrap_or(f64::NAN)),
        Value::String(s) => s.clone(),
        Value::Array(a) => a
            .iter()
            .map(|x| {
                if x.is_null() {
                    String::new()
                } else {
                    js_string(x)
                }
            })
            .collect::<Vec<_>>()
            .join(","),
        Value::Object(_) => "[object Object]".into(),
    }
}

/// `String(v ?? "")`.
pub fn js_string_or_empty(v: Option<&Value>) -> String {
    present(v).map(js_string).unwrap_or_default()
}

fn array_index(k: &str) -> Option<u32> {
    if k == "0" {
        return Some(0);
    }
    if k.is_empty() || k.starts_with('0') || !k.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    k.parse::<u32>().ok().filter(|n| *n != u32::MAX)
}

/// An object's keys in JS property order: array-index keys ascending, then insertion order.
pub fn keys(m: &Map<String, Value>) -> Vec<&String> {
    let mut idx: Vec<(u32, &String)> = m
        .keys()
        .filter_map(|k| array_index(k).map(|n| (n, k)))
        .collect();
    idx.sort_by_key(|(n, _)| *n);
    let mut out: Vec<&String> = idx.into_iter().map(|(_, k)| k).collect();
    out.extend(m.keys().filter(|k| array_index(k).is_none()));
    out
}

/// `Object.entries(v)` for an object or array value (anything else: none).
pub fn entries(v: &Value) -> Vec<(String, &Value)> {
    match v {
        Value::Object(m) => keys(m).into_iter().map(|k| (k.clone(), &m[k])).collect(),
        Value::Array(a) => a
            .iter()
            .enumerate()
            .map(|(i, x)| (i.to_string(), x))
            .collect(),
        _ => Vec::new(),
    }
}

/// `JSON.stringify(v)` bytes: JS key order, JS number text, serde's (identical) string escapes.
pub fn stringify(v: &Value) -> String {
    let mut out = String::new();
    write_json(v, &mut out);
    out
}

fn write_json(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            let f = n.as_f64().unwrap_or(f64::NAN);
            if f.is_finite() {
                out.push_str(&fmt_number(f));
            } else {
                out.push_str("null");
            }
        }
        Value::String(s) => out.push_str(&serde_json::to_string(s).unwrap_or_default()),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            out.push('{');
            for (i, k) in keys(m).into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&serde_json::to_string(k).unwrap_or_default());
                out.push(':');
                write_json(&m[k], out);
            }
            out.push('}');
        }
    }
}

// ------------------------------------------------------------------ dates ----

/// Days since 1970-01-01 for a proleptic Gregorian civil date (H. Hinnant's algorithm).
pub fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The civil date of a day number (inverse of [`days_from_civil`]).
pub fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (
        if m <= 2 {
            yoe + era * 400 + 1
        } else {
            yoe + era * 400
        },
        m,
        d,
    )
}

/// Milliseconds since the epoch, now (`Date.now()`).
pub fn now_ms() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_millis() as f64)
}

/// `new Date(ms).toISOString()` for four-digit years.
pub fn iso_from_ms(ms: f64) -> String {
    let ms = ms.floor() as i64;
    let days = ms.div_euclid(86_400_000);
    let rem = ms.rem_euclid(86_400_000);
    let (y, m, d) = civil_from_days(days);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{:03}Z",
        rem / 3_600_000,
        rem / 60_000 % 60,
        rem / 1000 % 60,
        rem % 1000
    )
}

pub fn iso_now() -> String {
    iso_from_ms(now_ms())
}

fn digits(s: &str, n: usize) -> Option<i64> {
    (s.len() == n && s.bytes().all(|b| b.is_ascii_digit()))
        .then(|| s.parse().ok())
        .flatten()
}

/// `Date.parse` for the ISO-8601 shapes this sidecar sees: `YYYY-MM-DD` optionally followed by
/// `THH:MM[:SS[.sss]]` and `Z` / `±HH:MM`. V8 accepts day 1-31 in every month and rolls the
/// overflow forward (measured: `2021-02-30` = 2021-03-02); month 1-12. Deviation: V8's
/// non-ISO fallback grammar is not ported — such strings read as NaN (`None`).
pub fn date_parse(s: &str) -> Option<f64> {
    let (date, time) = match s.split_once('T') {
        Some((d, t)) => (d, Some(t)),
        None => (s, None),
    };
    let mut parts = date.split('-');
    let y = digits(parts.next()?, 4)?;
    let mo = digits(parts.next()?, 2)?;
    let d = digits(parts.next()?, 2)?;
    if parts.next().is_some() || !(1..=12).contains(&mo) || !(1..=31).contains(&d) {
        return None;
    }
    let mut ms = days_from_civil(y, mo, d) * 86_400_000;
    if let Some(t) = time {
        let (clock, offset) = if let Some(c) = t.strip_suffix('Z') {
            (c, 0)
        } else if t.len() > 6 && matches!(t.as_bytes()[t.len() - 6], b'+' | b'-') {
            let (c, o) = t.split_at(t.len() - 6);
            let sign = if o.starts_with('-') { -1 } else { 1 };
            let oh = digits(&o[1..3], 2)?;
            let om = digits(&o[4..6], 2)?;
            if &o[3..4] != ":" {
                return None;
            }
            (c, sign * (oh * 60 + om) * 60_000)
        } else {
            // a date-time with no offset is local time in V8; this service runs in UTC.
            (t, 0)
        };
        let (hms, frac) = match clock.split_once('.') {
            Some((a, f)) => (a, Some(f)),
            None => (clock, None),
        };
        let mut f = hms.split(':');
        let h = digits(f.next()?, 2)?;
        let mi = digits(f.next()?, 2)?;
        let sec = match f.next() {
            Some(x) => digits(x, 2)?,
            None => 0,
        };
        if f.next().is_some() || h > 24 || mi > 59 || sec > 59 {
            return None;
        }
        let milli = match frac {
            Some(x) if !x.is_empty() && x.bytes().all(|b| b.is_ascii_digit()) => {
                let three: String = x.chars().chain("00".chars()).take(3).collect();
                three.parse::<i64>().ok()?
            }
            Some(_) => return None,
            None => 0,
        };
        ms += ((h * 60 + mi) * 60 + sec) * 1000 + milli - offset;
    }
    Some(ms as f64)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Every expected value measured with `node -e 'console.log(Number(s))'` on this host
    // (Node, 2026-10-18).
    #[test]
    fn js_number_matches_node() {
        let cases: &[(&str, f64)] = &[
            ("1e3", 1000.0),
            (" 42 ", 42.0),
            ("", 0.0),
            ("0x10", 16.0),
            ("0o17", 15.0),
            ("0b101", 5.0),
            ("0X1F", 31.0),
            ("+5", 5.0),
            ("1.", 1.0),
            ("0.5", 0.5),
            (".5", 0.5),
            ("\u{a0}42\u{feff}", 42.0),
            ("\n7\t", 7.0),
            ("1e-3", 0.001),
            ("00012", 12.0),
            ("+.5e+2", 50.0),
        ];
        for (s, want) in cases {
            assert_eq!(js_number(s), *want, "Number({s:?})");
        }
        for s in [
            "-0x10", ".", "1_000", "abc", "12px", "+", "1e", "-", "0x", "nan", "inf",
        ] {
            assert!(js_number(s).is_nan(), "Number({s:?}) is NaN");
        }
        assert_eq!(js_number("Infinity"), f64::INFINITY);
        assert_eq!(js_number("-Infinity"), f64::NEG_INFINITY);
        assert_eq!(js_number("1e400"), f64::INFINITY);
    }

    #[test]
    fn stringify_matches_node() {
        // node: JSON.stringify({a:1e21,b:1e-7,c:-0,d:1.5e-7,e:123456789012345680000,f:0.1+0.2,g:1e-6,h:2.5e300})
        let v: Value = serde_json::from_str(
            r#"{"a":1e21,"b":1e-7,"c":-0.0,"d":1.5e-7,"e":123456789012345680000,"f":0.30000000000000004,"g":0.000001,"h":2.5e300}"#,
        )
        .unwrap();
        assert_eq!(
            stringify(&v),
            r#"{"a":1e+21,"b":1e-7,"c":0,"d":1.5e-7,"e":123456789012345680000,"f":0.30000000000000004,"g":0.000001,"h":2.5e+300}"#
        );
        // node: JSON.stringify(JSON.parse('{"b":1,"10":2,"2":3,"a":4,"-1":5,"01":6}'))
        let v: Value = serde_json::from_str(r#"{"b":1,"10":2,"2":3,"a":4,"-1":5,"01":6}"#).unwrap();
        assert_eq!(stringify(&v), r#"{"2":3,"10":2,"b":1,"a":4,"-1":5,"01":6}"#);
        assert_eq!(stringify(&serde_json::json!(1.0)), "1");
    }

    #[test]
    fn dates_match_node() {
        // node: Date.parse("2021-02-30T00:00:00Z") === 1614643200000 (V8 rolls the day over)
        assert_eq!(
            date_parse("2021-02-30T00:00:00Z"),
            Some(1_614_643_200_000.0)
        );
        assert_eq!(
            date_parse("2020-02-29T00:00:00Z"),
            Some(1_582_934_400_000.0)
        );
        assert_eq!(date_parse("2021-13-01T00:00:00Z"), None);
        assert_eq!(date_parse("2021-01-00T00:00:00Z"), None);
        assert_eq!(
            iso_from_ms(date_parse("2026-10-13T23:59:59.999Z").unwrap()),
            "2026-10-13T23:59:59.999Z"
        );
    }
}
