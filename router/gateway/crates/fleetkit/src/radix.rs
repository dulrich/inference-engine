//! Validated radix parsing: the only fleet path to radix conversion.
//!
//! std's integer radix parsers accept a leading `+` (X1: `"+1"` in radix 16 parses as 1),
//! which JS never does; the fleet `clippy.toml` bans them in favour of [`parse_radix`].

/// The value of `c` as a digit of `radix` (2..=36), or `None`.
pub fn digit(c: u8, radix: u32) -> Option<u32> {
    let v = match c {
        b'0'..=b'9' => u32::from(c - b'0'),
        b'a'..=b'z' => u32::from(c - b'a') + 10,
        b'A'..=b'Z' => u32::from(c - b'A') + 10,
        _ => return None,
    };
    (v < radix).then_some(v)
}

/// `digits` as an unsigned integer in `radix` (2..=36). Every byte must be a digit of
/// `radix` (no sign, no whitespace, no separators); empty input or overflow → `None`.
pub fn parse_radix(digits: &str, radix: u32) -> Option<u64> {
    if !(2..=36).contains(&radix) || digits.is_empty() {
        return None;
    }
    let vals = digits
        .bytes()
        .map(|b| digit(b, radix))
        .collect::<Option<Vec<u32>>>()?;
    vals.into_iter().try_fold(0u64, |acc, d| {
        acc.checked_mul(u64::from(radix))?.checked_add(u64::from(d))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_every_digit() {
        assert_eq!(parse_radix("ff", 16), Some(255));
        assert_eq!(parse_radix("FF", 16), Some(255));
        assert_eq!(parse_radix("101", 2), Some(5));
        for s in ["", "+1", "-1", " 1", "1_0", "g", "2"] {
            let r = if s == "2" { 2 } else { 16 };
            assert_eq!(parse_radix(s, r), None, "{s:?}");
        }
    }

    #[test]
    fn overflow_is_none() {
        assert_eq!(parse_radix("ffffffffffffffff", 16), Some(u64::MAX));
        assert_eq!(parse_radix("10000000000000000", 16), None);
    }
}
