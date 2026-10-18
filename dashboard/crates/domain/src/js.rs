//! Small JS-semantics helpers the contract and routes depend on.

/// ECMAScript WhiteSpace + LineTerminator: the set `String.prototype.trim` strips and regex
/// `\s` matches.
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

/// `String.prototype.trim`.
pub fn trim(s: &str) -> &str {
    s.trim_matches(is_js_space)
}

/// The value of `c` as a digit of `radix` (2..=36), or None. Signs are never digits (X1).
fn digit(c: u8, radix: u32) -> Option<u32> {
    let v = match c {
        b'0'..=b'9' => u32::from(c - b'0'),
        b'a'..=b'z' => u32::from(c - b'a') + 10,
        b'A'..=b'Z' => u32::from(c - b'A') + 10,
        _ => return None,
    };
    (v < radix).then_some(v)
}

/// The value of a `0x`/`0o`/`0b` literal body as JS computes it: every byte must be a digit
/// of `radix` (no sign), accumulated in f64 so wide literals stay finite.
fn radix_literal(digits: &str, radix: u32) -> f64 {
    digits
        .bytes()
        .try_fold(0f64, |acc, b| {
            digit(b, radix).map(|d| acc * f64::from(radix) + f64::from(d))
        })
        .unwrap_or(f64::NAN)
}

/// Percent-decoding (`decodeURIComponent`): None on invalid UTF-8 or a malformed escape.
pub fn percent_decode(value: &str) -> Option<String> {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            // Validate each hex digit first: a sign is never a digit (X1, `%+1` fails).
            let hi = digit(*bytes.get(i + 1)?, 16)?;
            let lo = digit(*bytes.get(i + 2)?, 16)?;
            out.push((hi * 16 + lo) as u8);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

/// `Number.MAX_SAFE_INTEGER` (2^53 - 1).
pub const MAX_SAFE_INTEGER: i64 = 9_007_199_254_740_991;

/// A JSON number as `JSON.stringify` prints it: integral values without a fraction,
/// non-finite values as `null`.
pub fn json_number(v: f64) -> serde_json::Value {
    if v.fract() == 0.0 && v.abs() <= MAX_SAFE_INTEGER as f64 {
        serde_json::Value::from(v as i64)
    } else {
        serde_json::Number::from_f64(v).map_or(serde_json::Value::Null, serde_json::Value::Number)
    }
}

/// `Math.round`: the nearest integer, halves toward +infinity. Floor-based rather than
/// `(x + 0.5).floor()`, which rounds `0.49999999999999994` up to 1.
pub fn math_round(x: f64) -> f64 {
    let f = x.floor();
    if x - f >= 0.5 {
        f + 1.0
    } else {
        f
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn number_is_byte_safe_on_multibyte_prefixes() {
        for s in ["€1", "0€", "é1", "0x€", "€€"] {
            assert!(number(s).is_nan(), "{s}");
        }
        assert_eq!(number("0X1f"), 31.0);
    }

    #[test]
    fn radix_digits_are_validated_and_wide_literals_stay_finite() {
        assert_eq!(percent_decode("%+1"), None);
        assert_eq!(percent_decode("%2B"), Some("+".into()));
        assert!(number("0x+f").is_nan());
        assert!(number("0X-1").is_nan());
        assert!(number("0b+1").is_nan());
        assert_eq!(number("0x10000000000000000"), 18_446_744_073_709_552_000.0);
    }

    #[test]
    fn trims() {
        assert_eq!(trim("\u{FEFF} a\u{3000}\n"), "a");
    }

    #[test]
    fn json_numbers_and_rounding() {
        use serde_json::json;
        assert_eq!(json_number(3.0), json!(3));
        assert_eq!(json_number(-0.0), json!(0));
        assert_eq!(json_number(1.5), json!(1.5));
        assert_eq!(json_number(f64::NAN), json!(null));
        assert_eq!(json_number(f64::INFINITY), json!(null));
        assert_eq!(
            json_number(9_007_199_254_740_992.0),
            json!(9_007_199_254_740_992.0)
        );
        assert_eq!(
            json_number(9_007_199_254_740_991.0),
            json!(9_007_199_254_740_991_i64)
        );
        assert_eq!(math_round(2.5), 3.0);
        assert_eq!(math_round(-2.5), -2.0);
        assert_eq!(math_round(0.49999999999999994), 0.0);
    }
}

/// `Number(s)` for strings: trimmed, empty → 0, decimal/exponent/`0x`/`0o`/`0b`/`Infinity`
/// forms, anything else NaN.
pub fn number(s: &str) -> f64 {
    let s = trim(s);
    if s.is_empty() {
        return 0.0;
    }
    let radix = |p: &str, r| {
        let digits = s.get(2..).unwrap_or("");
        if s.len() > 2 && s.as_bytes()[..2].eq_ignore_ascii_case(p.as_bytes()) && !digits.is_empty()
        {
            Some(radix_literal(digits, r))
        } else {
            None
        }
    };
    if let Some(v) = radix("0x", 16)
        .or_else(|| radix("0o", 8))
        .or_else(|| radix("0b", 2))
    {
        return v;
    }
    match s {
        "Infinity" | "+Infinity" => return f64::INFINITY,
        "-Infinity" => return f64::NEG_INFINITY,
        _ => {}
    }
    if !s
        .bytes()
        .all(|b| b.is_ascii_digit() || matches!(b, b'.' | b'e' | b'E' | b'+' | b'-'))
        || !s.bytes().any(|b| b.is_ascii_digit())
    {
        return f64::NAN;
    }
    s.parse().unwrap_or(f64::NAN)
}

/// `x.replace(/\s+/g, " ")`.
pub fn collapse_space(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_space = false;
    for c in s.chars() {
        if is_js_space(c) {
            if !in_space {
                out.push(' ');
            }
            in_space = true;
        } else {
            out.push(c);
            in_space = false;
        }
    }
    out
}

#[cfg(test)]
mod number_tests {
    #[test]
    fn number_matches_js() {
        for (s, v) in [
            ("", 0.0),
            (" 12 ", 12.0),
            ("1e2", 100.0),
            ("0x10", 16.0),
            ("1.5", 1.5),
        ] {
            assert_eq!(super::number(s), v, "{s}");
        }
        for s in ["abc", "1a", "-", "inf", "."] {
            assert!(super::number(s).is_nan(), "{s}");
        }
    }
}
