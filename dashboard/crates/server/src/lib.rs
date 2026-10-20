//! bench-dashboard server: the Bun API's Rust replacement (plans/bench-dashboard-rust-port.md
//! WP-3..WP-5).
pub mod app;
pub mod check_recipes;
pub mod config;
pub mod control;
pub mod db;
pub mod eta;
pub mod health;
pub mod http;
pub mod ingest;
pub mod jsrt;
pub mod queue;
pub mod record;
pub mod router_feeds;
pub mod schema;
pub mod serving;
pub mod serving_live;
pub mod spa;
pub mod wilson;
