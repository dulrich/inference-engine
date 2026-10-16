//! Shared HTTP plumbing: app state, Hono-shaped JSON replies (typed DTOs -> `serde_json::Value`
//! -> `JSON.stringify` via the domain crate's `jsout`), and the error replies.
use crate::config::Config;
use crate::db::Db;
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use serde::Serialize;
use serde_json::json;
use std::sync::Arc;

pub struct AppState {
    pub cfg: Config,
    pub db: Db,
}

pub type Shared = Arc<AppState>;

/// `c.json(body, status)`: `JSON.stringify` text, `application/json`.
pub fn reply<T: Serialize + ?Sized>(status: StatusCode, body: &T) -> Response {
    match serde_json::to_value(body) {
        Ok(v) => (
            status,
            [(header::CONTENT_TYPE, "application/json")],
            crate::jsrt::stringify(&v),
        )
            .into_response(),
        Err(e) => RouteError::Internal(format!("reply JSON: {e}")).into_response(),
    }
}

pub fn ok<T: Serialize + ?Sized>(body: &T) -> Response {
    reply(StatusCode::OK, body)
}

pub fn error(status: StatusCode, message: &str) -> Response {
    reply(status, &json!({ "error": message }))
}

/// Every non-2xx a route returns, with the Bun API's status and body.
#[derive(Debug)]
pub enum RouteError {
    /// 400 `{ error }`.
    BadRequest(String),
    /// 404 `{ error }`.
    NotFound(String),
    /// 502 `{ error }` (a config read failure; the text is the runtime's).
    BadGateway(String),
    /// A thrown error in a Bun handler: Hono's default `onError`, a plain-text 500; the cause
    /// goes to stderr.
    Internal(String),
}

impl IntoResponse for RouteError {
    fn into_response(self) -> Response {
        match self {
            RouteError::BadRequest(m) => error(StatusCode::BAD_REQUEST, &m),
            RouteError::NotFound(m) => error(StatusCode::NOT_FOUND, &m),
            RouteError::BadGateway(m) => error(StatusCode::BAD_GATEWAY, &m),
            RouteError::Internal(cause) => {
                eprintln!("inference-dashboard: {cause}");
                internal_error()
            }
        }
    }
}

/// Hono's default `onError` reply.
pub fn internal_error() -> Response {
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        [(header::CONTENT_TYPE, "text/plain; charset=UTF-8")],
        "Internal Server Error",
    )
        .into_response()
}

impl From<String> for RouteError {
    fn from(e: String) -> Self {
        RouteError::Internal(e)
    }
}
