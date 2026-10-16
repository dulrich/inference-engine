//! JSON output exactly as the Bun API's `c.json(x)` (`JSON.stringify`) writes it.
//!
//! serde_json prints an `f64` through ryu (`1.0`, `1e-5`, `1e21`), which differs from JS
//! `Number#toString` (`1`, `0.00001`, `1e+21`). Every response body is therefore built as a
//! `serde_json::Value` and written by [`stringify`], which formats each number as JS does and
//! enumerates object keys in JS order (array-index keys first, ascending, then insertion order —
//! `JSON.parse` of a stored `by_tool` blob and a `perProject` record built from labels both
//! reorder that way in the Bun API).

/// `Number.prototype.toString()` (ECMA-262 Number::toString, radix 10): the shortest
/// round-trip digits, integral values without a fraction, exponent form outside
/// `1e-7 <= |x| < 1e21`, `-0` as `0`. Non-finite values return `None` (`JSON.stringify` → `null`).
pub fn number_to_string(x: f64) -> Option<String> {
    if !x.is_finite() {
        return None;
    }
    if x == 0.0 {
        return Some("0".into());
    }
    let sign = if x < 0.0 { "-" } else { "" };
    let (digits, n) = shortest_digits(x.abs())?;
    let k = digits.len() as i32;
    let body = if k <= n && n <= 21 {
        format!("{digits}{}", "0".repeat((n - k) as usize))
    } else if 0 < n && n <= 21 {
        format!("{}.{}", &digits[..n as usize], &digits[n as usize..])
    } else if -6 < n && n <= 0 {
        format!("0.{}{digits}", "0".repeat((-n) as usize))
    } else {
        let e = n - 1;
        let e_sign = if e < 0 { "-" } else { "+" };
        if k == 1 {
            format!("{digits}e{e_sign}{}", e.abs())
        } else {
            format!("{}.{}e{e_sign}{}", &digits[..1], &digits[1..], e.abs())
        }
    };
    Some(format!("{sign}{body}"))
}

/// The shortest round-trip significant digits `s` of a finite `x > 0` and the exponent `n` with
/// `x = 0.s × 10^n` (ECMA-262's `s`, `n`). Taken from ryu, never Rust's `{:e}`: when the value
/// is an exact decimal tie between two shortest candidates (e.g. `1792046177375.28125`), ECMA-262
/// and ryu pick the even digit (`…2812`, as Bun prints), while `{:e}` rounds up (`…2813`) —
/// which broke append-log cursor `ident` equality on Bun-written cursors (WP-7 differential).
fn shortest_digits(x: f64) -> Option<(String, i32)> {
    let mut buf = ryu::Buffer::new();
    let out = buf.format_finite(x);
    let (mantissa, exp) = match out.split_once('e') {
        Some((m, e)) => (m, e.parse::<i32>().ok()?),
        None => (out, 0),
    };
    let int_len = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let all: String = mantissa.chars().filter(|c| *c != '.').collect();
    let leading = all.len() - all.trim_start_matches('0').len();
    let digits = all[leading..].trim_end_matches('0').to_string();
    if digits.is_empty() {
        return None;
    }
    Some((digits, int_len - leading as i32 + exp))
}

/// `Number(x.toFixed(digits))` for `|x| < 1e21`: rounds the exact binary value, an exact tie
/// going to the larger magnitude (ECMA-262 "pick the larger n"), where Rust's `{:.N}` rounds
/// an exact tie half-to-even.
pub fn to_fixed_number(x: f64, digits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    let magnitude = x.abs();
    // Enough digits to print any tie exactly: a tie at `digits` places is a dyadic rational
    // with at most `digits + 1` decimals.
    let exact = format!("{:.*}", digits + 25, magnitude);
    let frac = exact.split_once('.').map_or("", |(_, f)| f);
    let tail = &frac[digits..];
    let tie = tail.starts_with('5') && tail[1..].bytes().all(|b| b == b'0');
    let rounded = if tie {
        let scale = 10f64.powi(digits as i32);
        ((magnitude * scale).floor() + 1.0) / scale
    } else {
        format!("{:.*}", digits, magnitude).parse().unwrap_or(0.0)
    };
    if x < 0.0 {
        -rounded
    } else {
        rounded
    }
}

/// An ECMAScript array index ("0", or a canonical decimal below 2^32 - 1).
pub fn is_array_index(key: &str) -> bool {
    let b = key.as_bytes();
    if b.is_empty() || b.len() > 10 || !b.iter().all(u8::is_ascii_digit) {
        return false;
    }
    if b.len() > 1 && b[0] == b'0' {
        return false;
    }
    key.parse::<u64>().is_ok_and(|n| n < 4_294_967_295)
}

/// `JSON.stringify(value)`: compact, numbers via [`number_to_string`], object keys in JS
/// enumeration order.
pub fn stringify(value: &serde_json::Value) -> String {
    let mut out = String::new();
    write_json(value, &mut out);
    out
}

fn push_str_json(s: &str, out: &mut String) {
    // serde_json escapes exactly JSON.stringify's set (`"`, `\`, U+0000..U+001F with the
    // `\b \f \n \r \t` short forms, lowercase `\u00xx` otherwise).
    out.push_str(&serde_json::to_string(s).unwrap_or_else(|_| "\"\"".into()));
}

fn write_json(value: &serde_json::Value, out: &mut String) {
    use serde_json::Value;
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                out.push_str(&u.to_string());
            } else {
                let s = n.as_f64().and_then(number_to_string);
                out.push_str(s.as_deref().unwrap_or("null"));
            }
        }
        Value::String(s) => push_str_json(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut index_keys: Vec<(u64, &String)> = map
                .keys()
                .filter(|k| is_array_index(k))
                .map(|k| (k.parse().unwrap_or(0), k))
                .collect();
            index_keys.sort();
            let ordered = index_keys
                .into_iter()
                .map(|(_, k)| k)
                .chain(map.keys().filter(|k| !is_array_index(k)));
            out.push('{');
            for (i, key) in ordered.enumerate() {
                if i > 0 {
                    out.push(',');
                }
                push_str_json(key, out);
                out.push(':');
                write_json(&map[key.as_str()], out);
            }
            out.push('}');
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// JSON numbers format as JS `Number#toString` does: shortest round-trip, integral floats
    /// without `.0`, exponent form only outside [1e-7, 1e21).
    #[test]
    #[allow(clippy::excessive_precision)] // deliberate JS-parity tie cases, values verbatim
    fn numbers_format_as_js() {
        for (x, want) in [
            (0.0, "0"),
            (-0.0, "0"),
            (1.0, "1"),
            (3.0e6, "3000000"),
            (0.1 + 0.2, "0.30000000000000004"),
            (1.5, "1.5"),
            (-2.25, "-2.25"),
            (0.000001, "0.000001"),
            (0.0000001, "1e-7"),
            (1.5e-7, "1.5e-7"),
            (1e21, "1e+21"),
            (1.2345e22, "1.2345e+22"),
            (123456789012345680000.0, "123456789012345680000"),
            (0.00005, "0.00005"),
            (12.5, "12.5"),
            (100.0, "100"),
            (5e-324, "5e-324"),
            // Exact decimal ties between two shortest candidates break to even, as Bun prints
            // (measured: `bun -e` on these values; birthtimeMs idents from the WP-7 corpus).
            (1792046177375.28125, "1792046177375.2812"),
            (1792046176921.90625, "1792046176921.9062"),
            (1791612307688.6348, "1791612307688.6348"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (9007199254740993.0, "9007199254740992"),
            (0.3, "0.3"),
            (0.001, "0.001"),
            (1.5e16, "15000000000000000"),
        ] {
            assert_eq!(number_to_string(x).unwrap(), want, "{x:e}");
        }
        assert_eq!(number_to_string(f64::NAN), None);
        assert_eq!(
            stringify(&json!({"a": 1.0, "b": [0.5, null, true, "x\n"], "c": 2, "d": f64::NAN})),
            r#"{"a":1,"b":[0.5,null,true,"x\n"],"c":2,"d":null}"#
        );
    }

    #[test]
    fn object_keys_enumerate_in_js_order() {
        let mut m = serde_json::Map::new();
        for k in ["b", "10", "a", "2", "01", "4294967295"] {
            m.insert(k.into(), json!(0));
        }
        assert_eq!(
            stringify(&serde_json::Value::Object(m)),
            r#"{"2":0,"10":0,"b":0,"a":0,"01":0,"4294967295":0}"#
        );
    }

    #[test]
    fn to_fixed_rounds_ties_to_the_larger_magnitude() {
        assert_eq!(to_fixed_number(0.03125, 4), 0.0313);
        assert_eq!(to_fixed_number(-0.03125, 4), -0.0313);
        assert_eq!(to_fixed_number(1.23456, 4), 1.2346);
        assert_eq!(to_fixed_number(1.00004, 4), 1.0);
        assert_eq!(to_fixed_number(2.5, 0), 3.0);
        assert_eq!(to_fixed_number(0.0, 4), 0.0);
        assert_eq!(to_fixed_number(12.34567891, 4), 12.3457);
    }
}
