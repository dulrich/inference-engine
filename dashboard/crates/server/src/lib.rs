//! bench-dashboard server (serving half): the serving/catalog/router API + SPA. The research
//! half (records, DB, queue, bake-off control) moved to eval-harness `inference_research`.
pub mod app;
pub mod check_recipes;
pub mod config;
pub mod control;
pub mod health;
pub mod http;
pub mod jsrt;
pub mod router_feeds;
pub mod serving;
pub mod serving_facts;
pub mod serving_live;
pub mod spa;
#[cfg(test)]
pub(crate) mod test_support;
