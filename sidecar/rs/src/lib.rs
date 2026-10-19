//! codex-sidecar (Rust port, plans/codex-sidecar-rust-port.md). One module per Node file in
//! `sidecar/`, named alike (`sidecar` is `codex-sidecar.mjs`, the composition root); `js`
//! holds the JavaScript-semantics helpers (`Number()`, `JSON.stringify`, truthiness, UTF-16
//! lengths) the port needs for byte/behaviour parity.

pub mod codex;
pub mod config;
pub mod errors;
pub mod http;
pub mod logging;
pub mod messages;
pub mod outbox;
pub mod outbox_events;
pub mod persona;
pub mod protocol;
pub mod request;
pub mod session;
pub mod sidecar;
pub mod sinks;
pub mod thread_config;
pub mod turn;
