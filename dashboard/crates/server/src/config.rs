//! One config object computed once from the environment. `X ?? default` semantics — a
//! set-but-empty variable is set. The optional dirs stay `None` when unset (the feature
//! switches). Repo-relative defaults resolve from this crate's source location: `webDir`
//! `dashboard/apps/web/dist` (the image sets `APP_WEB_DIR=/web`), `llamaConfigPath` under the
//! repo root.
use bd_domain::js;

const CRATE_DIR: &str = env!("CARGO_MANIFEST_DIR");

#[derive(Debug, Clone)]
pub struct Config {
    /// `Number(process.env.PORT ?? 4810)` — validated by the listener (`Bun.serve`'s range
    /// check), not here.
    pub port: f64,
    pub web_dir: String,
    pub llama_config_path: String,
    pub control_requests_dir: Option<String>,
    pub control_host_dir: Option<String>,
    pub router_telemetry_dir: Option<String>,
    pub codex_sidecar_log_dir: Option<String>,
    /// `SERVING_FEED_DIR`: the status collector's `serving-feed-*.jsonl` dir (`:ro`).
    pub serving_feed_dir: Option<String>,
    /// `SERVING_FACTS_PATH`: the research side's `serving-facts-v1` file (`:ro`).
    pub serving_facts_path: Option<String>,
}

pub type Env<'a> = &'a dyn Fn(&str) -> Option<String>;

fn env_var(name: &str) -> Option<String> {
    std::env::var_os(name).map(|v| v.to_string_lossy().into_owned())
}

impl Config {
    pub fn from_env() -> Config {
        Config::from_lookup(&env_var)
    }

    pub fn from_lookup(env: Env) -> Config {
        let or = |name: &str, fallback: String| env(name).unwrap_or(fallback);
        let dashboard = format!("{CRATE_DIR}/../..");
        Config {
            port: env("PORT").map_or(4810.0, |p| js::number(&p)),
            web_dir: or("APP_WEB_DIR", format!("{dashboard}/apps/web/dist")),
            llama_config_path: or(
                "LLAMA_CONFIG_PATH",
                format!("{dashboard}/../llama/config.yaml"),
            ),
            control_requests_dir: env("CONTROL_REQUESTS_DIR"),
            control_host_dir: env("CONTROL_HOST_DIR"),
            router_telemetry_dir: env("ROUTER_TELEMETRY_DIR"),
            codex_sidecar_log_dir: env("CODEX_SIDECAR_LOG_DIR"),
            serving_feed_dir: env("SERVING_FEED_DIR"),
            serving_facts_path: env("SERVING_FACTS_PATH"),
        }
    }
}

/// `Bun.serve({ port })`'s check: an integer in 0..=65535, else its `options.port` range error.
pub fn listen_port(port: f64) -> Result<u16, String> {
    if port.is_nan() || port.fract() != 0.0 {
        return Err(format!(
            "The value of \"options.port\" is out of range. It must be an integer. Received {}",
            bd_domain::jsout::number_to_string(port).unwrap_or_else(|| "NaN".into())
        ));
    }
    if !(0.0..=65535.0).contains(&port) {
        return Err(format!(
            "The value of \"options.port\" is out of range. It must be >= 0 and <= 65535. Received {}",
            bd_domain::jsout::number_to_string(port).unwrap_or_else(|| "Infinity".into())
        ));
    }
    Ok(port as u16)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(pairs: &[(&str, &str)]) -> Config {
        let owned: Vec<(String, String)> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        Config::from_lookup(&move |name| {
            owned
                .iter()
                .find(|(k, _)| k == name)
                .map(|(_, v)| v.clone())
        })
    }

    #[test]
    fn defaults_and_absence() {
        let c = cfg(&[]);
        assert_eq!(c.port, 4810.0);
        assert!(c.web_dir.ends_with("/apps/web/dist"));
        assert!(c.llama_config_path.ends_with("/llama/config.yaml"));
        assert_eq!(c.control_requests_dir, None);
        assert_eq!(c.router_telemetry_dir, None);
        assert_eq!(c.codex_sidecar_log_dir, None);
        assert_eq!(c.serving_feed_dir, None);
        assert_eq!(c.serving_facts_path, None);
    }

    #[test]
    fn set_values_win_even_when_empty() {
        let c = cfg(&[
            ("PORT", " 0x10 "),
            ("SERVING_FACTS_PATH", "/facts/serving-facts.json"),
            ("CONTROL_REQUESTS_DIR", ""),
        ]);
        assert_eq!(c.port, 16.0);
        assert_eq!(
            c.serving_facts_path.as_deref(),
            Some("/facts/serving-facts.json")
        );
        assert_eq!(c.control_requests_dir.as_deref(), Some(""));
        assert_eq!(cfg(&[("PORT", "")]).port, 0.0);
        assert!(cfg(&[("PORT", "abc")]).port.is_nan());
    }

    #[test]
    fn listen_port_is_bun_serve_range_checked() {
        assert_eq!(listen_port(4810.0), Ok(4810));
        assert_eq!(listen_port(0.0), Ok(0));
        assert!(listen_port(f64::NAN)
            .unwrap_err()
            .contains("must be an integer"));
        assert!(listen_port(1.5).unwrap_err().contains("must be an integer"));
        assert!(listen_port(70000.0)
            .unwrap_err()
            .contains(">= 0 and <= 65535"));
        assert!(listen_port(-1.0).unwrap_err().contains("Received -1"));
    }
}
