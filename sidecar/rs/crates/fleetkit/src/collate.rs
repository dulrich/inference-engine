//! Locale collation via `icu_collator` (Decision 11): one process-wide collator per mode,
//! built once, never per compare.
//!
//! - [`human_cmp`]: human-facing lists (labels, names, paths shown in a UI); root locale,
//!   default strength, numeric ordering ON (`file2 < file10`).
//! - [`stable_cmp`]: orders reaching a persisted or byte-compared sink; root locale, numeric
//!   off (ICU default, as JS `localeCompare`).
//!
//! Machine keys use plain `str::cmp`, not this module.

use icu_collator::options::CollatorOptions;
use icu_collator::preferences::CollationNumericOrdering;
use icu_collator::{Collator, CollatorBorrowed, CollatorPreferences};
use std::cmp::Ordering;
use std::sync::LazyLock;

fn build(numeric: CollationNumericOrdering) -> CollatorBorrowed<'static> {
    let mut prefs = CollatorPreferences::default();
    prefs.numeric_ordering = Some(numeric);
    Collator::try_new(prefs, CollatorOptions::default()).expect("compiled root collation data")
}

static HUMAN: LazyLock<CollatorBorrowed<'static>> =
    LazyLock::new(|| build(CollationNumericOrdering::True));
static STABLE: LazyLock<CollatorBorrowed<'static>> =
    LazyLock::new(|| build(CollationNumericOrdering::False));

/// Human-facing order: ICU root collation with numeric ordering.
pub fn human_cmp(a: &str, b: &str) -> Ordering {
    HUMAN.compare(a, b)
}

/// Stable order for K1 sinks: ICU root collation, numeric off (`a.localeCompare(b)`).
pub fn stable_cmp(a: &str, b: &str) -> Ordering {
    STABLE.compare(a, b)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cmp::Ordering::*;

    #[test]
    fn human_order() {
        assert_eq!(human_cmp("a", "B"), Less);
        assert_eq!(human_cmp("file2", "file10"), Less);
        assert_eq!(human_cmp("gpt-9", "gpt-10"), Less);
        assert_eq!(stable_cmp("file2", "file10"), Greater);
    }

    /// Agrees with token_burn's `locale_compare` vectors (`js.rs:234-238`) on ASCII.
    #[test]
    fn stable_matches_the_retired_shim() {
        assert_eq!(stable_cmp("/a/B", "/a/c"), Less);
        assert_eq!(stable_cmp("/a/b", "/a/b-x"), Less);
        assert_eq!(stable_cmp("/a/b-x", "/a/bx"), Less);
        assert_eq!(stable_cmp("/a/b_x", "/a/b-x"), Less);
        assert_eq!(stable_cmp("a", "A"), Less);
    }
}
