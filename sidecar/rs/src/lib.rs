//! codex-sidecar: an Anthropic-Messages front end over `codex app-server`, one child per
//! persona. `sidecar` is the composition root; `messages` is the request path (with
//! `request`, `disposition`, `turn` and `emission` as its phases); `persona` and `session`
//! own the app-server child and its threads; `codex` and `protocol` are the two wire
//! boundaries. The module map with one line per owner is in `sidecar/README.md`.

pub mod codex;
pub mod config;
pub mod disposition;
pub mod emission;
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
