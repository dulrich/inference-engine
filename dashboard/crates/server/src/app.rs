//! The serving dashboard's routes: health, serving config + timing/measured (from the
//! `serving-facts-v1` file), serving control and the catalog drop-box, router status, then the
//! JSON 404 under `/api/*` and the SPA fallback. The research routes (experiments, ladders,
//! curves, queue, bake-off control, capture export) moved to eval-harness `inference_research`
//! and answer the JSON 404 here. Registration order is irrelevant (no overlapping patterns).
use crate::http::{internal_error, ok, reply, RouteError, Shared};
use crate::jsrt;
use crate::serving::read_serving_config;
use crate::spa;
use crate::{control, router_feeds, serving_facts, serving_live};
use axum::body::Body;
use axum::body::Bytes;
use axum::extract::{RawQuery, Request, State};
use axum::http::{header, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::Router;
use serde::Serialize;
use serde_json::json;

/// Run a DB/disk-bound route body on the blocking pool; a panicked task is the 500 path.
async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> Result<T, RouteError> + Send + 'static,
) -> Result<T, RouteError> {
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| RouteError::Internal(e.to_string()))?
}

pub fn router(state: Shared) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/api/serving/timing", get(serving_timing))
        .route("/api/serving/measured", get(serving_measured))
        .route("/api/serving/facts-status", get(serving_facts_status))
        .route("/api/serving", get(serving))
        .route("/api/serving/status", get(serving_status))
        .route("/api/serving/live", get(serving_live))
        .route("/api/serving/visibility", post(serving_visibility))
        .route("/api/catalog/add", post(catalog_add))
        .route("/api/catalog/remove", post(catalog_remove))
        .route("/api/catalog/consumers", post(catalog_consumers))
        .route("/api/router/telemetry", get(router_telemetry))
        .route("/api/router/sidecar", get(router_sidecar))
        // A known path with an unrouted method falls through to Hono's `app.all("/api/*")`
        // JSON 404, not a 405.
        .method_not_allowed_fallback(fallback)
        .fallback(fallback)
        .with_state(state)
}

#[derive(Serialize)]
struct HealthResponse {
    status: &'static str,
}

/// `routes/health.ts`; no DB since the research half moved out.
async fn health() -> Response {
    ok(&HealthResponse { status: "ok" })
}

/// Read the facts file on the blocking pool; every state is a 200 body.
async fn facts(
    s: Shared,
    project: fn(&FactsResult) -> serde_json::Value,
) -> Result<Response, RouteError> {
    blocking(move || {
        let f = serving_facts::read_serving_facts(
            s.cfg.serving_facts_path.as_deref(),
            &s.cfg.llama_config_path,
        );
        Ok(ok(&project(&f)))
    })
    .await
}

type FactsResult = Result<serving_facts::Facts, serving_facts::Unavailable>;

/// `GET /api/serving/timing`: `ServingTimingRow[]` from the facts file, `[]` when unavailable.
async fn serving_timing(State(s): State<Shared>) -> Result<Response, RouteError> {
    facts(s, serving_facts::timing).await
}

/// `GET /api/serving/measured`: `ServingMeasuredEntry[]` from the facts file, `[]` when unavailable.
async fn serving_measured(State(s): State<Shared>) -> Result<Response, RouteError> {
    facts(s, serving_facts::measured).await
}

/// `GET /api/serving/facts-status`: `ServingFactsStatusResponse`, always 200.
async fn serving_facts_status(State(s): State<Shared>) -> Result<Response, RouteError> {
    facts(s, serving_facts::status).await
}

// ---- WP-4: file planes -------------------------------------------------------------------

/// `routes/serving.ts`: any failure (read or parse) is the 502 `{error}`.
async fn serving(State(s): State<Shared>) -> Result<Response, RouteError> {
    blocking(move || {
        let body = read_serving_config(&s.cfg.llama_config_path).map_err(RouteError::BadGateway)?;
        Ok(ok(&body))
    })
    .await
}

/// `routes/servingControl.ts` `GET /api/serving/status`: always 200.
async fn serving_status(State(s): State<Shared>) -> Result<Response, RouteError> {
    blocking(move || {
        Ok(ok(&control::read_serving_status(
            s.cfg.control_host_dir.as_deref(),
            jsrt::now_ms(),
        )))
    })
    .await
}

/// `GET /api/serving/live` (WP-6): the status collector's feed, always 200.
async fn serving_live(State(s): State<Shared>) -> Result<Response, RouteError> {
    blocking(move || {
        Ok(ok(&serving_live::build_serving_live_response(
            s.cfg.serving_feed_dir.as_deref(),
            jsrt::now_ms(),
        )))
    })
    .await
}

fn action_reply(r: control::ActionResult) -> Response {
    reply(
        StatusCode::from_u16(r.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        &r.body,
    )
}

type Action = fn(
    Option<&str>,
    &serde_json::Value,
    control::IdFactory,
) -> Result<control::ActionResult, String>;

/// A drop-box POST: `readJsonBody`, then the action against `CONTROL_REQUESTS_DIR`; a thrown
/// write error is the plain 500.
async fn drop_box(
    s: Shared,
    body: Bytes,
    action: Action,
    ids: fn() -> std::io::Result<String>,
) -> Result<Response, RouteError> {
    blocking(move || {
        let parsed = control::read_json_body(&body);
        let mut factory = ids;
        let r = action(s.cfg.control_requests_dir.as_deref(), &parsed, &mut factory)?;
        Ok(action_reply(r))
    })
    .await
}

async fn serving_visibility(State(s): State<Shared>, body: Bytes) -> Result<Response, RouteError> {
    drop_box(
        s,
        body,
        control::record_serving_visibility_action,
        control::generate_serving_request_id,
    )
    .await
}

async fn catalog_add(State(s): State<Shared>, body: Bytes) -> Result<Response, RouteError> {
    drop_box(
        s,
        body,
        control::record_catalog_add_action,
        control::generate_serving_request_id,
    )
    .await
}

async fn catalog_remove(State(s): State<Shared>, body: Bytes) -> Result<Response, RouteError> {
    drop_box(
        s,
        body,
        control::record_catalog_remove_action,
        control::generate_serving_request_id,
    )
    .await
}

async fn catalog_consumers(State(s): State<Shared>, body: Bytes) -> Result<Response, RouteError> {
    drop_box(
        s,
        body,
        control::record_catalog_consumers_action,
        control::generate_serving_request_id,
    )
    .await
}

/// `c.req.query("staleMs")` -> `parseStaleMsQuery`.
fn stale_ms(q: Option<&str>) -> Option<f64> {
    let first = jsrt::query_all(q, "staleMs").and_then(|v| v.into_iter().next());
    router_feeds::parse_stale_ms_query(first.as_deref())
}

async fn router_telemetry(
    State(s): State<Shared>,
    RawQuery(q): RawQuery,
) -> Result<Response, RouteError> {
    blocking(move || {
        Ok(ok(&router_feeds::build_router_telemetry_response(
            s.cfg.router_telemetry_dir.as_deref(),
            stale_ms(q.as_deref()),
            jsrt::now_ms(),
        )))
    })
    .await
}

async fn router_sidecar(State(s): State<Shared>) -> Result<Response, RouteError> {
    blocking(move || {
        Ok(ok(&router_feeds::build_router_sidecar_response(
            s.cfg.codex_sidecar_log_dir.as_deref(),
            jsrt::now_ms(),
        )))
    })
    .await
}

fn is_api(path: &str) -> bool {
    path == "/api" || path.starts_with("/api/")
}

async fn fallback(State(state): State<Shared>, req: Request) -> Response {
    let path = req.uri().path().to_string();
    if is_api(&path) {
        return reply(StatusCode::NOT_FOUND, &json!({ "error": "not found" }));
    }
    let web = std::path::Path::new(&state.cfg.web_dir);
    // `app.get("*")` is only registered when the web dir exists; Hono's default 404 otherwise.
    if (req.method() != Method::GET && req.method() != Method::HEAD) || !web.exists() {
        return (StatusCode::NOT_FOUND, "404 Not Found").into_response();
    }
    let Some(file) = spa::resolve(web, &path) else {
        return internal_error();
    };
    let cache = if file == web.join("index.html") {
        "no-cache"
    } else {
        "public, max-age=31536000, immutable"
    };
    match tokio::fs::read(&file).await {
        Ok(bytes) => Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, spa::content_type(&file))
            .header(header::CACHE_CONTROL, cache)
            .body(Body::from(bytes))
            .unwrap_or_else(|_| internal_error()),
        Err(e) => {
            eprintln!("inference-dashboard: spa {}: {e}", file.display());
            internal_error()
        }
    }
}

#[cfg(test)]
#[path = "app_tests.rs"]
mod tests;
