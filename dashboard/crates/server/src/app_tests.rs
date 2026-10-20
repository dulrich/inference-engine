//! Route-level checks through the axum router (`app.routes.test.ts` / `spa-fallback.test.ts`
//! intent); the black-box harness is the parity oracle.
use super::*;
use crate::config::Config;
use crate::db::Db;
use crate::http::AppState;
use crate::record::tests::{fixtures, TempDir};
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

fn app(t: &TempDir, llama: &str) -> Router {
    let mut cfg = Config::from_lookup(&|_| None);
    cfg.db_path = format!("{}/bench.db", t.0);
    cfg.web_dir = format!("{}/web", t.0);
    cfg.llama_config_path = llama.to_string();
    t.write("web/index.html", "<html>spa</html>");
    t.write("web/assets/app-1234.js", "js");
    let db = Db::open(&cfg.db_path).unwrap();
    crate::ingest::run_ingest(&db, &fixtures("records"), false, |_, _| {}).unwrap();
    router(Arc::new(AppState { cfg, db }))
}

#[tokio::test]
async fn routes_and_errors() {
    let t = TempDir::new("app");
    let app = app(&t, &fixtures("llama-config.yaml"));
    let (s, ty, _, b) = call(&app, "GET", "/health").await;
    assert_eq!(
        (s, ty.as_str(), b.as_str()),
        (
            200,
            "application/json",
            r#"{"status":"ok","experimentCount":2}"#
        )
    );
    let (s, _, _, b) = call(&app, "GET", "/api/experiments").await;
    assert_eq!(s, 200);
    assert_eq!(j(&b).as_array().unwrap().len(), 2);
    let (s, _, _, b) = call(&app, "GET", "/api/experiments/fixture-v2-full").await;
    assert_eq!(s, 200);
    let d = j(&b);
    assert!(d["trials"].is_array() && d["metrics"].is_array() && d["wilsonSummary"].is_object());
    let (s, _, _, b) = call(&app, "GET", "/api/experiments/nope").await;
    assert_eq!((s, b.as_str()), (404, r#"{"error":"not found"}"#));
    let (s, _, _, b) = call(&app, "GET", "/api/curves/fixture-v2-full").await;
    assert_eq!(s, 200);
    let c = j(&b);
    assert_eq!(c["experimentId"], "fixture-v2-full");
    assert_eq!(c["points"].as_array().unwrap().len(), 2);
    assert_eq!(c["points"][0]["sided"], "two");
    let (_, _, _, b) = call(&app, "GET", "/api/curves/nope").await;
    assert_eq!(b, r#"{"experimentId":"nope","points":[]}"#);
    let (s, _, _, b) = call(&app, "GET", "/api/ladders/").await;
    assert_eq!((s, b.as_str()), (400, r#"{"error":"hfRepo is required"}"#));
    let (s, _, _, b) = call(&app, "GET", "/api/ladders").await;
    assert_eq!((s, b.as_str()), (200, "[]"));
    let (s, _, _, b) = call(&app, "GET", "/api/ladders/example-org%2FExample-Tiny-GGUF").await;
    assert_eq!(s, 200);
    assert_eq!(j(&b).as_array().unwrap().len(), 1);
    let (s, _, _, b) = call(&app, "GET", "/api/ladders/example-org/Example-Tiny-GGUF").await;
    assert_eq!((s, j(&b).as_array().unwrap().len()), (200, 1));
    let (s, ty, _, _) = call(&app, "GET", "/api/ladders/%E0%A4%A").await;
    assert_eq!((s, ty.as_str()), (500, "text/plain; charset=UTF-8"));
    let (s, _, _, b) = call(&app, "GET", "/api/serving/timing").await;
    assert_eq!(s, 200, "{b}");
    assert!(j(&b).is_array());
    let (s, _, _, b) = call(
        &app,
        "GET",
        "/api/export/scoreboard-capture?id=fixture-v2-smoke-cell&id=fixture-v2-full&id=nope",
    )
    .await;
    assert_eq!(s, 200);
    let e = j(&b);
    assert_eq!(
        (
            e["captureVersion"].clone(),
            e["stability"].clone(),
            e["recordCount"].clone()
        ),
        (1.into(), "v1".into(), 2.into())
    );
    assert_eq!(e["records"][0]["experimentId"], "fixture-v2-smoke-cell");
    // WP-6: live residency is disabled (typed 200) without SERVING_FEED_DIR; slots retired.
    let (s, _, _, b) = call(&app, "GET", "/api/serving/live").await;
    assert_eq!(
        (s, b.as_str()),
        (200, r#"{"available":false,"reason":"disabled"}"#)
    );
    for (m, p) in [
        ("GET", "/api/router/slots"),
        ("GET", "/api/nope"),
        ("GET", "/api"),
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

#[tokio::test]
async fn config_failures_are_502() {
    let t = TempDir::new("app502");
    let app = app(&t, "/absent/config.yaml");
    for p in ["/api/serving/timing", "/api/export/scoreboard-capture"] {
        let (s, _, _, b) = call(&app, "GET", p).await;
        assert_eq!(s, 502, "{p}");
        assert!(j(&b)["error"]
            .as_str()
            .unwrap()
            .contains("/absent/config.yaml"));
    }
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
