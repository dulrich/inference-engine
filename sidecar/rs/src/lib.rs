//! codex-sidecar (Rust port, plans/codex-sidecar-rust-port.md). One module per Node file in
//! `sidecar/`, named alike; `js` holds the JavaScript-semantics helpers (`Number()`,
//! `JSON.stringify`, truthiness, UTF-16 lengths) the port needs for byte/behaviour parity.

pub mod config;
pub mod errors;
pub mod js;
pub mod logging;
pub mod outbox;
pub mod outbox_events;
pub mod protocol;
pub mod request;
pub mod thread_config;
