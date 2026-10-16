//! `apps/api/src/app.ts` for the record-derived routes (WP-3): health, experiments, ladders,
//! curves, serving timing and the capture export, then the JSON 404 under `/api/*` and the SPA
//! fallback. The file-plane routes (serving, serving control, queue, control, router status)
//! land in WP-4.
use crate::db::{CaptureRecord, ExperimentSummary};
use crate::http::{internal_error, ok, reply, RouteError, Shared};
use crate::jsrt;
use crate::serving::read_served_models;
use crate::spa;
use crate::wilson::{wilson_interval, Sided};
use axum::body::Body;
use axum::extract::{RawQuery, Request, State};
use axum::http::{header, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
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
        .route("/api/experiments", get(list_experiments))
        .route("/api/experiments/{id}", get(experiment_detail))
        .route("/api/ladders", get(ladders))
        .route("/api/ladders/", get(ladders))
        .route("/api/ladders/{*rest}", get(ladders))
        .route("/api/curves/{id}", get(curves))
        .route("/api/serving/timing", get(serving_timing))
        .route("/api/export/scoreboard-capture", get(capture_export))
        // A known path with an unrouted method falls through to Hono's `app.all("/api/*")`
        // JSON 404, not a 405.
        .method_not_allowed_fallback(fallback)
        .fallback(fallback)
        .with_state(state)
}

/// The last path segment as Hono's `c.req.param(name)` reads it.
fn last_param(req_path: &str) -> String {
    jsrt::hono_param(req_path.rsplit('/').next().unwrap_or(""))
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct HealthResponse {
    status: &'static str,
    experiment_count: f64,
}

/// `routes/health.ts`.
async fn health(State(s): State<Shared>) -> Result<Response, RouteError> {
    blocking(move || {
        Ok(ok(&HealthResponse {
            status: "ok",
            experiment_count: s.db.experiment_count()?,
        }))
    })
    .await
}

/// `routes/experiments.ts`.
async fn list_experiments(State(s): State<Shared>) -> Result<Response, RouteError> {
    blocking(move || {
        let list: Vec<ExperimentSummary> = s.db.list_experiment_summaries()?;
        Ok(ok(&list))
    })
    .await
}

async fn experiment_detail(State(s): State<Shared>, req: Request) -> Result<Response, RouteError> {
    let id = last_param(req.uri().path());
    blocking(move || match s.db.experiment_full_detail(&id)? {
        Some(detail) => Ok(ok(&detail)),
        None => Err(RouteError::NotFound("not found".into())),
    })
    .await
}

/// `routes/ladders.ts`: everything after `/api/ladders/` of Hono's decoded path, then
/// `decodeURIComponent` (a throw is the plain 500). `/api/ladders` itself matches the wildcard
/// and, the prefix regex not applying, looks up the repo `"/api/ladders"`.
async fn ladders(State(s): State<Shared>, req: Request) -> Result<Response, RouteError> {
    let path = jsrt::hono_path(req.uri().path());
    let rest = path.strip_prefix("/api/ladders/").unwrap_or(&path);
    let Some(hf_repo) = jsrt::decode_uri_component(rest) else {
        return Err(RouteError::Internal(format!(
            "URIError: URI error ({rest})"
        )));
    };
    if hf_repo.is_empty() {
        return Err(RouteError::BadRequest("hfRepo is required".into()));
    }
    blocking(move || Ok(ok(&s.db.list_ladder_rows(&hf_repo)?))).await
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CurveSeriesPoint {
    task: String,
    position_fraction: f64,
    n: f64,
    k: f64,
    failure_rate: f64,
    sided: &'static str,
    confidence: f64,
    lower: Option<f64>,
    upper: f64,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CurveSeries {
    experiment_id: String,
    points: Vec<CurveSeriesPoint>,
}

/// `routes/curves.ts`: one Wilson-scored point per `(task, position_fraction)` cell, every
/// non-`"correct"` verdict a failure, sorted by `task.localeCompare` then position.
async fn curves(State(s): State<Shared>, req: Request) -> Result<Response, RouteError> {
    let experiment_id = last_param(req.uri().path());
    blocking(move || {
        struct Group {
            key: String,
            task: String,
            position_fraction: f64,
            n: f64,
            k: f64,
        }
        let mut groups: Vec<Group> = Vec::new();
        for t in s.db.list_longcontext_trials(&experiment_id)? {
            let (Some(task), Some(pf)) = (t.task, t.position_fraction) else {
                continue;
            };
            let key = format!(
                "{task}::{}",
                bd_domain::jsout::number_to_string(pf).unwrap_or_default()
            );
            let i = match groups.iter().position(|g| g.key == key) {
                Some(i) => i,
                None => {
                    groups.push(Group {
                        key,
                        task,
                        position_fraction: pf,
                        n: 0.0,
                        k: 0.0,
                    });
                    groups.len() - 1
                }
            };
            groups[i].n += 1.0;
            if t.classifier_verdict.as_deref() != Some("correct") {
                groups[i].k += 1.0;
            }
        }
        groups.sort_by(|a, b| {
            jsrt::locale_compare(&a.task, &b.task).then_with(|| {
                (a.position_fraction - b.position_fraction)
                    .partial_cmp(&0.0)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
        });
        let points = groups
            .into_iter()
            .map(|g| {
                let w = wilson_interval(g.k, g.n, Sided::Two, 0.95)?;
                Ok(CurveSeriesPoint {
                    failure_rate: g.k / g.n,
                    task: g.task,
                    position_fraction: g.position_fraction,
                    n: g.n,
                    k: g.k,
                    sided: w.sided.as_str(),
                    confidence: w.confidence,
                    lower: w.lower,
                    upper: w.upper,
                })
            })
            .collect::<Result<Vec<_>, String>>()?;
        Ok(ok(&CurveSeries {
            experiment_id,
            points,
        }))
    })
    .await
}

/// `routes/servingTiming.ts`: the config is read per request; any failure inside the handler's
/// `try` (config read/parse or DB) is the 502 `{error}`.
async fn serving_timing(State(s): State<Shared>) -> Result<Response, RouteError> {
    blocking(move || {
        let served =
            read_served_models(&s.cfg.llama_config_path).map_err(RouteError::BadGateway)?;
        let rows =
            s.db.list_serving_timing_rows(&served)
                .map_err(RouteError::BadGateway)?;
        Ok(ok(&rows))
    })
    .await
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CaptureEnvelope {
    capture_version: u8,
    stability: &'static str,
    generated_at: String,
    record_count: usize,
    records: Vec<CaptureRecord>,
}

/// `routes/export.ts`: the frozen v1 capture envelope, `?id=` repeatable (query order), every
/// record resolved against one config snapshot. Any failure inside the handler's `try` —
/// config read or DB — is the 502 `{error}`.
async fn capture_export(
    State(s): State<Shared>,
    RawQuery(q): RawQuery,
) -> Result<Response, RouteError> {
    blocking(move || {
        let served =
            read_served_models(&s.cfg.llama_config_path).map_err(RouteError::BadGateway)?;
        let ids = match jsrt::query_all(q.as_deref(), "id") {
            Some(ids) => ids,
            None => s.db.list_experiment_ids().map_err(RouteError::BadGateway)?,
        };
        let mut records = Vec::new();
        for id in &ids {
            if let Some(r) =
                s.db.capture_record(id, &served)
                    .map_err(RouteError::BadGateway)?
            {
                records.push(r);
            }
        }
        Ok(ok(&CaptureEnvelope {
            capture_version: 1,
            stability: "v1",
            generated_at: jsrt::iso_now(),
            record_count: records.len(),
            records,
        }))
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
