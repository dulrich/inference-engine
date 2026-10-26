//! `fleetkit` — shared helpers for fleet Rust services, one canonical copy each.
//!
//! Every module is a cargo feature (default: none); see README.md for the table.

#[cfg(feature = "collate")]
pub mod collate;
#[cfg(feature = "js")]
pub mod js;
#[cfg(feature = "jsout")]
pub mod jsout;
#[cfg(feature = "query")]
pub mod query;
#[cfg(feature = "radix")]
pub mod radix;
#[cfg(feature = "shutdown")]
pub mod shutdown;
