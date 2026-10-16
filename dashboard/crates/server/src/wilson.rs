//! `apps/api/src/analysis/wilson.ts`: the Wilson score interval on a failure rate, `sided`
//! required, only 95% tabulated (anything else is an error, never a guessed z-value).

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sided {
    One,
    Two,
}

impl Sided {
    pub fn as_str(self) -> &'static str {
        match self {
            Sided::One => "one",
            Sided::Two => "two",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct WilsonInterval {
    pub sided: Sided,
    pub confidence: f64,
    pub lower: Option<f64>,
    pub upper: f64,
}

fn z(sided: Sided, confidence: f64) -> Option<f64> {
    (confidence == 0.95).then_some(match sided {
        Sided::One => 1.644_853_626_951_472_2,
        Sided::Two => 1.959_963_984_540_054,
    })
}

fn clamp01(v: f64) -> f64 {
    v.clamp(0.0, 1.0)
}

/// `wilsonInterval(k, n, sided, confidence)`.
pub fn wilson_interval(
    k: f64,
    n: f64,
    sided: Sided,
    confidence: f64,
) -> Result<WilsonInterval, String> {
    let z = z(sided, confidence).ok_or_else(|| {
        format!(
            "wilsonInterval: confidence {} is not tabulated for sided=\"{}\"",
            bd_domain::jsout::number_to_string(confidence).unwrap_or_else(|| "NaN".into()),
            sided.as_str()
        )
    })?;
    if n <= 0.0 {
        return Err("wilsonInterval: n must be positive".into());
    }
    let phat = k / n;
    let z2 = z * z;
    let denominator = 1.0 + z2 / n;
    let center = phat + z2 / (2.0 * n);
    let adjustment = z * (phat * (1.0 - phat) / n + z2 / (4.0 * n * n)).sqrt();
    let upper = clamp01((center + adjustment) / denominator);
    if sided == Sided::One {
        return Ok(WilsonInterval {
            sided,
            confidence,
            lower: None,
            upper,
        });
    }
    let lower = clamp01((center - adjustment) / denominator);
    Ok(WilsonInterval {
        sided,
        confidence,
        lower: Some(lower),
        upper,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `analysis/wilson.test.ts`, plus exact values (measured with `bun -e` on wilson.ts).
    #[test]
    fn wilson_cases() {
        let one = wilson_interval(0.0, 30.0, Sided::One, 0.95).unwrap();
        assert_eq!(one.lower, None);
        assert!((one.upper - 0.0827).abs() < 0.0005);
        let two = wilson_interval(2.0, 4.0, Sided::Two, 0.95).unwrap();
        assert!(two.lower.unwrap() < 0.5 && two.upper > 0.5);
        assert!(wilson_interval(1.0, 10.0, Sided::Two, 0.9).is_err());
        assert_eq!(
            wilson_interval(0.0, 0.0, Sided::Two, 0.95).unwrap_err(),
            "wilsonInterval: n must be positive"
        );
        assert_eq!(
            wilson_interval(1.0, 10.0, Sided::Two, 0.9).unwrap_err(),
            "wilsonInterval: confidence 0.9 is not tabulated for sided=\"two\""
        );
        // Bounds clamp to [0, 1] at the extremes.
        let all = wilson_interval(5.0, 5.0, Sided::Two, 0.95).unwrap();
        assert!(all.upper <= 1.0 && all.lower.unwrap() > 0.0);
        assert_eq!(
            wilson_interval(0.0, 5.0, Sided::Two, 0.95).unwrap().lower,
            Some(0.0)
        );
        // Exact arithmetic parity with the TS formula.
        let exact = wilson_interval(3.0, 7.0, Sided::Two, 0.95).unwrap();
        assert_eq!(exact.lower, Some(0.15821985525146973));
        assert_eq!(exact.upper, 0.7495416354723429);
        assert_eq!(one.upper, 0.08272430812509929);
        assert_eq!(all.lower, Some(0.5655175352168252));
        assert_eq!(all.upper, 1.0);
    }
}
