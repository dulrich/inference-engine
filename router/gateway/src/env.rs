//! The one environment reader: trimmed strings, one bool spelling set, native integer parses.
//! Unset or empty always means "not given".

use std::str::FromStr;

pub type Env<'a> = &'a dyn Fn(&str) -> Option<String>;

/// The trimmed value of `name`, `None` when unset or empty.
pub fn string(env: Env, name: &str) -> Option<String> {
    trimmed(env(name))
}

pub fn trimmed(v: Option<String>) -> Option<String> {
    v.map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

/// `true|1|yes|on` / `false|0|no|off`, case-insensitive; anything else is `None`.
pub fn parse_bool(s: &str) -> Option<bool> {
    match s.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Some(true),
        "false" | "0" | "no" | "off" => Some(false),
        _ => None,
    }
}

/// A plain decimal integer (ASCII digits only, so no sign, exponent, fraction or `inf`) that
/// fits `T`. `Ok(None)` when unset or empty; `Err` names the variable.
pub fn integer<T: FromStr>(env: Env, name: &str) -> Result<Option<T>, String> {
    let Some(s) = string(env, name) else {
        return Ok(None);
    };
    if s.bytes().all(|b| b.is_ascii_digit()) {
        if let Ok(n) = s.parse::<T>() {
            return Ok(Some(n));
        }
    }
    Err(format!(
        "{name}={s:?} is not a valid {} (plain decimal integer in range)",
        std::any::type_name::<T>()
    ))
}

/// For fail-open knobs: the bool, else `fallback` with one stderr warning when set but invalid.
pub fn flag_or_warn(env: Env, name: &str, fallback: bool) -> bool {
    match string(env, name) {
        None => fallback,
        Some(s) => parse_bool(&s).unwrap_or_else(|| {
            eprintln!("warning: {name}={s:?} is not a boolean; using {fallback}");
            fallback
        }),
    }
}

/// For fail-open knobs: a positive integer, else `fallback` with one stderr warning when set
/// but invalid (including 0).
pub fn positive_or_warn<T: FromStr + PartialOrd + Default + std::fmt::Display + Copy>(
    env: Env,
    name: &str,
    fallback: T,
) -> T {
    match integer::<T>(env, name) {
        Ok(None) => fallback,
        Ok(Some(n)) if n > T::default() => n,
        Ok(Some(_)) | Err(_) => {
            eprintln!("warning: {name} is not a positive integer; using {fallback}");
            fallback
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integer_is_strict_decimal() {
        let get = |v: &'static str| move |_: &str| Some(v.to_string());
        assert_eq!(integer::<u16>(&get(" 4813 "), "PORT"), Ok(Some(4813)));
        assert_eq!(integer::<u16>(&get(""), "PORT"), Ok(None));
        for bad in ["70000", "4813.9", "1e3", "inf", "-1", "+1", "NaN"] {
            let e = integer::<u16>(&get(bad), "PORT").unwrap_err();
            assert!(e.starts_with("PORT="), "{e}");
        }
        assert_eq!(positive_or_warn::<u64>(&get("0"), "X", 7), 7);
        assert!(!flag_or_warn(&get("nope"), "X", false));
    }
}
