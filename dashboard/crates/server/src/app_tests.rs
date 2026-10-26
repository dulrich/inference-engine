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

fn facts_body(revision: &str, generated_at: &str, model: &str) -> String {
    serde_json::json!({
        "schema": "serving-facts-v1",
        "generatedAt": generated_at,
        "servingConfigRevision": revision,
        "timing": [{
            "modelKey": model, "aliases": ["m-a"], "p50PrefillTokS": 1.5, "p90PrefillTokS": null,
            "p50DecodeTokS": null, "p90DecodeTokS": null, "samplingArm": null,
            "sampleCount": 1, "mtp": true
        }],
        "measured": [{"alias": "m-a", "experimentId": format!("exp-{model}")}]
    })
    .to_string()
}

/// `GET /api/serving/facts`: always a 200 JSON body.
async fn get_facts(app: &Router) -> Value {
    let (s, ty, _, b) = call(app, "GET", "/api/serving/facts").await;
    assert_eq!((s, ty.as_str()), (200, "application/json"), "{b}");
    j(&b)
}

#[tokio::test]
async fn facts_route_states() {
    let t = TempDir::new("facts-app");
    t.write("config.yaml", FACTS_CONFIG);
    let llama = format!("{}/config.yaml", t.0);
    let facts = format!("{}/facts.json", t.0);
    let rev = sha256_hex(FACTS_CONFIG.as_bytes());
    let unavailable = |reason: &str| serde_json::json!({"available": false, "reason": reason, "researchPort": 4816});
    // disabled
    assert_eq!(get_facts(&app(&t, &llama)).await, unavailable("disabled"));
    let with_facts = || app_with(&t, &llama, Some(facts.clone()));
    // absent
    assert_eq!(get_facts(&with_facts()).await, unavailable("absent"));
    // present
    t.write("facts.json", &facts_body(&rev, "2026-10-25T12:00:00Z", "m"));
    let body = get_facts(&with_facts()).await;
    assert_eq!(
        body,
        serde_json::json!({
            "available": true,
            "generatedAt": "2026-10-25T12:00:00Z",
            "servingConfigRevision": rev,
            "timing": [{
                "modelKey": "m", "aliases": ["m-a"], "p50PrefillTokS": 1.5, "p90PrefillTokS": null,
                "p50DecodeTokS": null, "p90DecodeTokS": null, "samplingArm": null,
                "sampleCount": 1, "mtp": true
            }],
            "measured": [{"alias": "m-a", "experimentId": "exp-m"}],
            "researchPort": 4816,
        })
    );
    // invalid
    t.write("facts.json", r#"{"schema":"serving-facts-v1"}"#);
    assert_eq!(get_facts(&with_facts()).await, unavailable("invalid"));
    // stale-config
    t.write(
        "facts.json",
        &facts_body(&sha256_hex(b"an older config"), "2026-10-25T12:00:00Z", "m"),
    );
    assert_eq!(get_facts(&with_facts()).await, unavailable("stale-config"));
    // the research port comes from server config
    let mut cfg = Config::from_lookup(&|_| None);
    cfg.llama_config_path = llama.clone();
    cfg.research_port = 5816;
    let ported = router(Arc::new(AppState { cfg }));
    assert_eq!(get_facts(&ported).await["researchPort"], 5816);
    // the three split routes are gone: the JSON 404
    for p in [
        "/api/serving/timing",
        "/api/serving/measured",
        "/api/serving/facts-status",
    ] {
        let (s, ty, _, b) = call(&with_facts(), "GET", p).await;
        assert_eq!(
            (s, ty.as_str(), b.as_str()),
            (404, "application/json", r#"{"error":"not found"}"#),
            "{p}"
        );
    }
}

/// One generation per response: availability, timing and measured come from one read of the
/// file (`serving_facts::serve` reads once and builds the body from that read). Rewriting the
/// file between requests yields responses that are each internally consistent.
#[tokio::test]
async fn facts_response_is_one_generation() {
    let t = TempDir::new("facts-gen");
    t.write("config.yaml", FACTS_CONFIG);
    let llama = format!("{}/config.yaml", t.0);
    let facts = format!("{}/facts.json", t.0);
    let rev = sha256_hex(FACTS_CONFIG.as_bytes());
    let app = app_with(&t, &llama, Some(facts));
    for (gen, model) in [
        ("2026-10-25T12:00:00Z", "gen-a"),
        ("2026-10-25T13:00:00Z", "gen-b"),
    ] {
        t.write("facts.json", &facts_body(&rev, gen, model));
        let body = get_facts(&app).await;
        assert_eq!(body["generatedAt"], gen);
        assert_eq!(body["timing"][0]["modelKey"], model);
        assert_eq!(body["measured"][0]["experimentId"], format!("exp-{model}"));
    }
    // A generation that turns stale carries no lists from an earlier read.
    t.write("config.yaml", "models: {changed: {}}\n");
    let body = get_facts(&app).await;
    assert_eq!(body["reason"], "stale-config");
    assert!(body.get("timing").is_none() && body.get("measured").is_none());
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
