//! `health.ts`.
use super::*;

pub const HEALTH_RESPONSE: Export = Export::out(
    "HealthResponse",
    obj(&[f("status", &Node::Lit(Lit::Str("ok")))]),
);
