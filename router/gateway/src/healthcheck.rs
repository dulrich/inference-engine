//! `inference-router healthcheck` — the compose healthcheck contract
//! (`fleet-tools/fleet/compose.fleet.yml:849-869`): authenticate to the codex sidecar's
//! `/healthz` with `CODEX_SIDECAR_KEY`, fail unless the JSON body's `status` is `"ok"`, 4 s
//! timeout, no metered turn; additionally fail if this gateway's own `/health` does not answer.

use crate::config::{self, Env};
use serde_json::Value;
use std::time::Duration;

pub const SIDECAR_URL_ENV: &str = "INFERENCE_ROUTER_HEALTHCHECK_SIDECAR_URL";
pub const DEFAULT_SIDECAR_URL: &str = "http://host.docker.internal:4877/healthz";
pub const TIMEOUT: Duration = Duration::from_secs(4);

async fn status_ok(
    client: &reqwest::Client,
    url: &str,
    bearer: Option<&str>,
) -> Result<(), String> {
    let mut req = client.get(url);
    if let Some(key) = bearer {
        req = req.header("authorization", format!("Bearer {key}"));
    }
    let resp = req.send().await.map_err(|e| format!("{url}: {e}"))?;
    let text = resp.text().await.map_err(|e| format!("{url}: {e}"))?;
    let body: Value =
        serde_json::from_str(&text).map_err(|_| format!("{url}: non-JSON body: {text}"))?;
    if body.get("status").and_then(Value::as_str) == Some("ok") {
        Ok(())
    } else {
        Err(format!("{url}: {text}"))
    }
}

pub async fn check(
    self_url: &str,
    sidecar_url: &str,
    sidecar_key: Option<&str>,
    timeout: Duration,
) -> Result<(), String> {
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(timeout)
        .build()
        .map_err(|e| e.to_string())?;
    status_ok(&client, self_url, None).await?;
    status_ok(&client, sidecar_url, sidecar_key).await
}

pub async fn run(env: Env<'_>) -> Result<(), String> {
    // Same strict `PORT` parse as the server: an invalid value fails the check loudly.
    let port = crate::env::integer::<u16>(env, "PORT")?
        .or_else(|| config::load(env).ok().map(|s| s.port))
        .unwrap_or(3000);
    let sidecar = env(SIDECAR_URL_ENV)
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_SIDECAR_URL.into());
    let key = env("CODEX_SIDECAR_KEY");
    check(
        &format!("http://127.0.0.1:{port}/health"),
        &sidecar,
        key.as_deref(),
        TIMEOUT,
    )
    .await
}
