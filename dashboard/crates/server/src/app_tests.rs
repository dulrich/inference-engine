//! Route-level checks through the axum router (`app.routes.test.ts` / `spa-fallback.test.ts`
//! intent); the black-box harness is the parity oracle.
use super::*;
use crate::config::Config;
use crate::http::AppState;
use crate::serving_facts::sha256_hex;
use crate::test_support::{fixtures, TempDir};
use http_body_util::BodyExt;
use serde_json::Value;
use std::sync::Arc;
use tower::ServiceExt;

async fn call(app: &Router, method: &str, uri: &str) -> (u16, String, String, String) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .body(Body::empty())
        .unwrap();
    let res = app.clone().oneshot(req).await.unwrap();
    let status = res.status().as_u16();
    let h = |name| {
        res.headers()
            .get(name)
            .map(|v: &axum::http::HeaderValue| v.to_str().unwrap_or("").to_string())
            .unwrap_or_default()
    };
    let (ty, cache) = (h(header::CONTENT_TYPE), h(header::CACHE_CONTROL));
    let bytes = res.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        ty,
        cache,
        String::from_utf8_lossy(&bytes).into_owned(),
    )
}

fn j(body: &str) -> Value {
    serde_json::from_str(body).unwrap_or_else(|e| panic!("{e}: {body}"))
}

fn app_with(t: &TempDir, llama: &str, facts: Option<String>) -> Router {
    let mut cfg = Config::from_lookup(&|_| None);
    cfg.web_dir = format!("{}/web", t.0);
    cfg.llama_config_path = llama.to_string();
    cfg.serving_facts_path = facts;
    t.write("web/index.html", "<html>spa</html>");
    t.write("web/assets/app-1234.js", "js");
    router(Arc::new(AppState { cfg }))
}

fn app(t: &TempDir, llama: &str) -> Router {
    app_with(t, llama, None)
}

#[tokio::test]
async fn routes_and_errors() {
    let t = TempDir::new("app");
    let app = app(&t, &fixtures("llama-config.yaml"));
    let (s, ty, _, b) = call(&app, "GET", "/health").await;
    assert_eq!(
        (s, ty.as_str(), b.as_str()),
        (200, "application/json", r#"{"status":"ok"}"#)
    );
    // WP-6: live residency is disabled (typed 200) without SERVING_FEED_DIR; slots retired.
    let (s, _, _, b) = call(&app, "GET", "/api/serving/live").await;
    assert_eq!(
        (s, b.as_str()),
        (200, r#"{"available":false,"reason":"disabled"}"#)
    );
    // The research half moved to eval-harness: its routes are the JSON 404 here.
    for (m, p) in [
        ("GET", "/api/router/slots"),
        ("GET", "/api/nope"),
        ("GET", "/api"),
        ("GET", "/api/experiments"),
        ("GET", "/api/experiments/fixture-v2-full"),
        ("GET", "/api/ladders"),
        ("GET", "/api/ladders/example-org/Example-Tiny-GGUF"),
        ("GET", "/api/curves/fixture-v2-full"),
        ("GET", "/api/queue"),
        ("GET", "/api/export/scoreboard-capture"),
        ("POST", "/api/control/arm"),
        ("POST", "/api/control/disarm"),
        ("POST", "/api/control/stop"),
        ("POST", "/api/control/start"),
        ("POST", "/api/control/cancel"),
        ("POST", "/api/experiments"),
        ("DELETE", "/api/queue/x"),
    ] {
        let (s, ty, _, b) = call(&app, m, p).await;
        assert_eq!(
            (s, ty.as_str(), b.as_str()),
            (404, "application/json", r#"{"error":"not found"}"#),
            "{m} {p}"
        );
    }
}

const FACTS_CONFIG: &str = "models: {}\n";

fn facts_body(revision: &str) -> String {
    serde_json::json!({
        "schema": "serving-facts-v1",
        "generatedAt": "2026-10-25T12:00:00Z",
        "servingConfigRevision": revision,
        "timing": [{
            "modelKey": "m", "aliases": ["m-a"], "p50PrefillTokS": 1.5, "p90PrefillTokS": null,
            "p50DecodeTokS": null, "p90DecodeTokS": null, "samplingArm": null,
            "sampleCount": 1, "mtp": true
        }],
        "measured": [{"alias": "m-a", "experimentId": "exp-1"}]
    })
    .to_string()
}

async fn facts_routes(app: &Router) -> (String, String, Value) {
    let mut out = Vec::new();
    for p in [
        "/api/serving/timing",
        "/api/serving/measured",
        "/api/serving/facts-status",
    ] {
        let (s, ty, _, b) = call(app, "GET", p).await;
        assert_eq!((s, ty.as_str()), (200, "application/json"), "{p}: {b}");
        out.push(b);
    }
    let status = j(&out[2]);
    (out.remove(0), out.remove(0), status)
}

#[tokio::test]
async fn facts_states_for_timing_and_measured() {
    let t = TempDir::new("facts-app");
    t.write("config.yaml", FACTS_CONFIG);
    let llama = format!("{}/config.yaml", t.0);
    let facts = format!("{}/facts.json", t.0);
    // disabled
    let (timing, measured, status) = facts_routes(&app(&t, &llama)).await;
    assert_eq!((timing.as_str(), measured.as_str()), ("[]", "[]"));
    assert_eq!(
        status,
        serde_json::json!({"available": false, "reason": "disabled"})
    );
    let with_facts = || app_with(&t, &llama, Some(facts.clone()));
    // absent
    let (timing, measured, status) = facts_routes(&with_facts()).await;
    assert_eq!((timing.as_str(), measured.as_str()), ("[]", "[]"));
    assert_eq!(status["reason"], "absent");
    // present
    t.write(
        "facts.json",
        &facts_body(&sha256_hex(FACTS_CONFIG.as_bytes())),
    );
    let (timing, measured, status) = facts_routes(&with_facts()).await;
    assert_eq!(j(&timing)[0]["modelKey"], "m");
    assert_eq!(j(&timing)[0]["mtp"], true);
    assert_eq!(measured, r#"[{"alias":"m-a","experimentId":"exp-1"}]"#);
    assert_eq!(
        status,
        serde_json::json!({
            "available": true,
            "generatedAt": "2026-10-25T12:00:00Z",
            "servingConfigRevision": sha256_hex(FACTS_CONFIG.as_bytes()),
        })
    );
    // invalid
    t.write("facts.json", r#"{"schema":"serving-facts-v1"}"#);
    let (timing, measured, status) = facts_routes(&with_facts()).await;
    assert_eq!((timing.as_str(), measured.as_str()), ("[]", "[]"));
    assert_eq!(status["reason"], "invalid");
    // stale-config
    t.write("facts.json", &facts_body(&sha256_hex(b"an older config")));
    let (timing, measured, status) = facts_routes(&with_facts()).await;
    assert_eq!((timing.as_str(), measured.as_str()), ("[]", "[]"));
    assert_eq!(status["reason"], "stale-config");
}

#[tokio::test]
async fn config_failures_are_502() {
    let t = TempDir::new("app502");
    let app = app(&t, "/absent/config.yaml");
    let (s, _, _, b) = call(&app, "GET", "/api/serving").await;
    assert_eq!(s, 502);
    assert!(j(&b)["error"]
        .as_str()
        .unwrap()
        .contains("/absent/config.yaml"));
}

#[tokio::test]
async fn spa_cache_headers_and_fallback() {
    let t = TempDir::new("spa");
    let app = app(&t, &fixtures("llama-config.yaml"));
    let (s, ty, cache, b) = call(&app, "GET", "/").await;
    assert_eq!(
        (s, ty.as_str(), cache.as_str(), b.as_str()),
        (
            200,
            "text/html;charset=utf-8",
            "no-cache",
            "<html>spa</html>"
        )
    );
    let (s, _, cache, b) = call(&app, "GET", "/assets/app-1234.js").await;
    assert_eq!(
        (s, cache.as_str(), b.as_str()),
        (200, "public, max-age=31536000, immutable", "js")
    );
    for p in [
        "/runs",
        "/serving/x",
        "/assets/missing-0000.js",
        "/..%2f..%2fetc%2fpasswd",
        "/%2e%2e%2fpackage.json",
        "/assets/..%2f..%2f..%2fpackage.json",
    ] {
        let (s, _, cache, b) = call(&app, "GET", p).await;
        assert_eq!(
            (s, cache.as_str(), b.as_str()),
            (200, "no-cache", "<html>spa</html>"),
            "{p}"
        );
    }
    let (s, _, _, _) = call(&app, "GET", "/%E0%A4%A").await;
    assert_eq!(s, 500);
}
