//! `GET /api/serving/live` (plans/serving-control-redesign.md WP-6): live model residency per
//! tier from the host status collector's `serving-feed-YYYY-MM-DD.jsonl` (`SERVING_FEED_DIR`,
//! mounted `:ro`). A bounded 1 MiB tail read of today's file (else the newest day) — never a
//! whole-file read, never outbound HTTP. Every reply is 200: the payload, or a typed
//! `{available:false, reason:"disabled"|"unreadable"|"empty"|"stale"}`.
use crate::jsrt;
use crate::router_feeds::{parse_jsonl_lines, select_daily, tail_of, DEFAULT_TAIL_WINDOW_BYTES};
use bd_domain::contract::{SERVING_FEED_LINE, SERVING_LIVE_STALE_MS};
use serde_json::{json, Map, Value};

pub const SERVING_FEED_PREFIX: &str = "serving-feed-";
/// The newest load/unload events returned, newest first.
pub const MAX_RECENT_EVENTS: usize = 50;
const TIERS: [&str; 2] = ["gpu", "onnx"];

/// `serving-feed-YYYY-MM-DD.jsonl`: today's UTC day, else the newest day present.
pub fn select_serving_feed_file(names: &[String], now_ms: f64) -> Option<String> {
    select_daily(names, SERVING_FEED_PREFIX, now_ms)
}

fn project_model(raw: &Value) -> Option<Value> {
    let model = raw.get("model")?.as_str()?;
    let num = |k: &str| raw.get(k).filter(|v| v.is_number()).cloned();
    Some(json!({
        "model": model,
        "state": raw.get("state").filter(|v| v.is_string()).cloned().unwrap_or(Value::Null),
        "ttl": num("ttl").unwrap_or(Value::Null),
        "preload": raw.get("preload").filter(|v| v.is_boolean()).cloned().unwrap_or(Value::Null),
        "idle_unload_seconds": num("idle_unload_seconds").unwrap_or(Value::Null),
    }))
}

fn tier_status(snapshot: Option<&Value>) -> Value {
    let Some(s) = snapshot else {
        return json!({"available": false, "sampled_at": null, "reason": "no snapshot in feed"});
    };
    let ts = s.get("ts").cloned().unwrap_or(Value::Null);
    if s.get("available") == Some(&Value::Bool(true)) {
        let models: Vec<Value> = s
            .get("models")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(project_model)
            .collect();
        json!({"available": true, "sampled_at": ts, "models": models})
    } else {
        let reason = s
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("unavailable");
        json!({"available": false, "sampled_at": ts, "reason": reason})
    }
}

/// Build the `/api/serving/live` reply.
pub fn build_serving_live_response(dir: Option<&str>, now_ms: f64) -> Value {
    let (file, lines) = match tail_of(
        dir,
        select_serving_feed_file,
        DEFAULT_TAIL_WINDOW_BYTES,
        now_ms,
    ) {
        Ok(x) => x,
        Err(reply) => return reply,
    };
    let (records, _) = parse_jsonl_lines(&lines, &SERVING_FEED_LINE);
    let mut latest: Map<String, Value> = Map::new();
    let mut events: Vec<Value> = Vec::new();
    let mut newest_ms: Option<f64> = None;
    for r in &records {
        let tier = r.get("tier").and_then(Value::as_str).unwrap_or("");
        let ts = r.get("ts").and_then(Value::as_str).unwrap_or("");
        match r.get("kind").and_then(Value::as_str) {
            Some("snapshot") => {
                if let Some(ms) = jsrt::date_parse(ts) {
                    newest_ms = Some(newest_ms.map_or(ms, |n: f64| n.max(ms)));
                }
                latest.insert(tier.to_string(), r.clone());
            }
            Some(kind @ ("load" | "unload")) => {
                if let Some(model) = r.get("model").and_then(Value::as_str) {
                    events.push(json!({"ts": ts, "tier": tier, "kind": kind, "model": model}));
                }
            }
            _ => {}
        }
    }
    let Some(newest) = newest_ms else {
        return json!({"available": false, "reason": "empty"});
    };
    if now_ms - newest > SERVING_LIVE_STALE_MS as f64 {
        return json!({"available": false, "reason": "stale"});
    }
    let recent: Vec<Value> = events.into_iter().rev().take(MAX_RECENT_EVENTS).collect();
    let mut out = Map::new();
    out.insert("available".into(), Value::Bool(true));
    out.insert(
        "generated_at".into(),
        Value::String(jsrt::iso_from_ms(now_ms as i64)),
    );
    out.insert("file".into(), Value::String(file));
    for tier in TIERS {
        out.insert(tier.into(), tier_status(latest.get(tier)));
    }
    out.insert("recent".into(), Value::Array(recent));
    Value::Object(out)
}

#[cfg(test)]
#[path = "serving_live_tests.rs"]
mod tests;
