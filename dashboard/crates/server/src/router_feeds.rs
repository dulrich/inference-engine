//! The router-status plane: `apps/api/src/router/{tailReader,currentFile,telemetryFeed,
//! sidecarFeed}.ts` and `routes/routerStatus.ts`'s `parseStaleMsQuery`. Every jsonl read is a
//! bounded byte-window tail (1 MiB) — never a whole-file read; every reply is 200,
//! `{available: false, reason}` when off/unreadable/empty. No outbound HTTP. (The slots feed
//! retired at plans/serving-control-redesign.md WP-6; residency is `serving_live.rs`.)
use crate::jsrt;
use bd_domain::contract::{
    Export, ROUTER_PROGRESS_RECORD, ROUTER_REQUEST_RECORD, ROUTER_SIDECAR_RECORD,
};
use bd_domain::validate;
use serde::Serialize;
use serde_json::{json, Map, Value};
use std::io::{Read, Seek, SeekFrom};

/// 1 MiB — the telemetry and sidecar window.
pub const DEFAULT_TAIL_WINDOW_BYTES: u64 = 1024 * 1024;

#[derive(Debug, Clone, PartialEq)]
pub struct TailRead {
    pub lines: Vec<String>,
    pub size: u64,
    pub window_bytes: u64,
    pub truncated: bool,
}

/// `readTailLines`: at most the last `window` bytes; the straddled first line dropped when the
/// window was truncated (no newline in a truncated window -> no lines); a trailing fragment
/// kept for the parser to reject. `Bun.file(path).size` reads 0 for a file it cannot stat, and
/// a zero size is never read.
pub fn read_tail_lines(path: &str, window: u64) -> std::io::Result<TailRead> {
    let size = std::fs::metadata(path).map_or(0, |m| m.len());
    let start = size.saturating_sub(window);
    let truncated = start > 0;
    let text = if size == 0 {
        String::new()
    } else {
        let mut f = std::fs::File::open(path)?;
        f.seek(SeekFrom::Start(start))?;
        let mut buf = Vec::with_capacity((size - start) as usize);
        f.take(size - start).read_to_end(&mut buf)?;
        String::from_utf8_lossy(&buf).into_owned()
    };
    let lines = if truncated && !text.contains('\n') {
        Vec::new()
    } else {
        let body = if truncated {
            &text[text.find('\n').map_or(0, |i| i + 1)..]
        } else {
            &text[..]
        };
        body.split('\n')
            .filter(|l| !l.is_empty())
            .map(str::to_string)
            .collect()
    };
    Ok(TailRead {
        lines,
        size,
        window_bytes: window,
        truncated,
    })
}

/// `parseJsonlLines`: (records, skipped).
pub fn parse_jsonl_lines(lines: &[String], schema: &Export) -> (Vec<Value>, usize) {
    let mut records = Vec::new();
    let mut skipped = 0;
    for line in lines {
        match jsrt::json_parse(line)
            .ok()
            .and_then(|raw| validate::parse(&schema.node, &raw).ok())
        {
            Some(v) => records.push(v),
            None => skipped += 1,
        }
    }
    (records, skipped)
}

// ---- currentFile.ts -----------------------------------------------------------------------

/// `utcDayStamp`.
pub fn utc_day_stamp(now_ms: f64) -> String {
    jsrt::iso_from_ms(now_ms as i64)[..10].to_string()
}

/// `listDirOrNull`.
pub fn list_dir(dir: &str) -> Option<Vec<String>> {
    Some(
        std::fs::read_dir(dir)
            .ok()?
            .filter_map(Result::ok)
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect(),
    )
}

/// `\d{4}-\d{2}-\d{2}` (ASCII digits).
fn is_day(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 10
        && b.iter().enumerate().all(|(i, c)| match i {
            4 | 7 => *c == b'-',
            _ => c.is_ascii_digit(),
        })
}

fn pick(candidates: Vec<(String, String, f64)>, today: &str) -> Option<String> {
    let has_today = candidates.iter().any(|c| c.1 == today);
    let mut best: Option<&(String, String, f64)> = None;
    for c in candidates.iter().filter(|c| !has_today || c.1 == today) {
        best = match best {
            Some(b) if !(c.1 > b.1 || (c.1 == b.1 && c.2 > b.2)) => Some(b),
            _ => Some(c),
        };
    }
    best.map(|b| b.0.clone())
}

/// `selectRouterTelemetryFile`: today's UTC day (else the newest day), highest NUMERIC index.
pub fn select_router_telemetry_file(names: &[String], now_ms: f64) -> Option<String> {
    let candidates = names
        .iter()
        .filter_map(|n| {
            let rest = n.strip_prefix("router-")?.strip_suffix(".jsonl")?;
            let (day, index) = rest.split_at_checked(10)?;
            let index = index.strip_prefix('.')?;
            (is_day(day) && !index.is_empty() && index.bytes().all(|b| b.is_ascii_digit())).then(
                || {
                    (
                        n.clone(),
                        day.to_string(),
                        index.parse::<f64>().unwrap_or(0.0),
                    )
                },
            )
        })
        .collect();
    pick(candidates, &utc_day_stamp(now_ms))
}

pub(crate) fn select_daily(names: &[String], prefix: &str, now_ms: f64) -> Option<String> {
    let candidates = names
        .iter()
        .filter_map(|n| {
            let day = n.strip_prefix(prefix)?.strip_suffix(".jsonl")?;
            is_day(day).then(|| (n.clone(), day.to_string(), 0.0))
        })
        .collect();
    pick(candidates, &utc_day_stamp(now_ms))
}

/// `selectSidecarFile`.
pub fn select_sidecar_file(names: &[String], now_ms: f64) -> Option<String> {
    select_daily(names, "codex-sidecar-", now_ms)
}

// ---- routerStatus.ts ----------------------------------------------------------------------

pub const STALE_MS_MIN: f64 = 1000.0;
pub const STALE_MS_MAX: f64 = 600_000.0;

/// `parseStaleMsQuery`: absent, non-numeric, non-finite or out-of-range is ignored (None).
pub fn parse_stale_ms_query(raw: Option<&str>) -> Option<f64> {
    let v = bd_domain::js::number(raw?);
    (v.is_finite() && (STALE_MS_MIN..=STALE_MS_MAX).contains(&v)).then_some(v)
}

pub(crate) fn unavailable(reason: &str) -> Value {
    json!({ "available": false, "reason": reason })
}

/// Shared prologue of the feeds: dir -> selected file -> tail lines, or the reply.
pub(crate) fn tail_of(
    dir: Option<&str>,
    select: fn(&[String], f64) -> Option<String>,
    window: u64,
    now_ms: f64,
) -> Result<(String, Vec<String>), Value> {
    let Some(dir) = dir.filter(|d| !d.is_empty()) else {
        return Err(unavailable("disabled"));
    };
    let Some(names) = list_dir(dir) else {
        return Err(unavailable("unreadable"));
    };
    let Some(file) = select(&names, now_ms) else {
        return Err(unavailable("empty"));
    };
    match read_tail_lines(&jsrt::path_join(dir, &file), window) {
        Ok(t) => Ok((file, t.lines)),
        Err(_) => Err(unavailable("unreadable")),
    }
}

// ---- telemetryFeed.ts ---------------------------------------------------------------------

pub const DEFAULT_TELEMETRY_STALE_MS: f64 = 15_000.0;
pub const DEFAULT_MAX_RECENT: usize = 200;

fn num(v: &Value, k: &str) -> Option<f64> {
    v.get(k).and_then(Value::as_f64)
}

fn str_or_null(v: &Value, k: &str) -> Value {
    v.get(k).cloned().unwrap_or(Value::Null)
}

/// `median` (even counts average the middle pair).
fn median(values: &[f64]) -> Option<f64> {
    crate::eta::median_of(values)
}

/// `RouterLegSummary`.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LegSummary {
    pub leg: Option<String>,
    pub requests: f64,
    pub errors: f64,
    pub p50_ttft_ms: Option<f64>,
}

/// `summariseLegs`: one bucket per `leg` (the `null` leg its own bucket), sorted by request
/// count desc then `(leg ?? "").localeCompare`.
pub fn summarise_legs(recent: &[Value]) -> Vec<LegSummary> {
    let mut buckets: Vec<(Option<String>, f64, f64, Vec<f64>)> = Vec::new();
    for r in recent {
        let leg = r.get("leg").and_then(Value::as_str).map(str::to_string);
        let i = match buckets.iter().position(|b| b.0 == leg) {
            Some(i) => i,
            None => {
                buckets.push((leg, 0.0, 0.0, Vec::new()));
                buckets.len() - 1
            }
        };
        let b = &mut buckets[i];
        b.1 += 1.0;
        // `record.error !== null`: an absent (undefined) `error` counts as non-null.
        let is_error = num(r, "status").is_some_and(|s| s >= 400.0)
            || r.get("error").is_none_or(|e| !e.is_null());
        if is_error {
            b.2 += 1.0;
        }
        if let Some(t) = num(r, "ttftMs") {
            b.3.push(t);
        }
    }
    let mut out: Vec<LegSummary> = buckets
        .into_iter()
        .map(|(leg, requests, errors, ttft)| LegSummary {
            leg,
            requests,
            errors,
            p50_ttft_ms: median(&ttft),
        })
        .collect();
    out.sort_by(|a, b| {
        b.requests.total_cmp(&a.requests).then_with(|| {
            jsrt::locale_compare(
                a.leg.as_deref().unwrap_or(""),
                b.leg.as_deref().unwrap_or(""),
            )
        })
    });
    out
}

/// `deriveInFlight`: progress-only `reqId`s not aged out, newest-active first.
pub fn derive_in_flight(
    progress: &[Value],
    terminal: &std::collections::HashSet<String>,
    now_ms: f64,
    stale_ms: f64,
) -> Vec<Value> {
    let mut by_req: Vec<(String, Vec<&Value>)> = Vec::new();
    for r in progress {
        let id = r.get("reqId").and_then(Value::as_str).unwrap_or("");
        if terminal.contains(id) {
            continue;
        }
        match by_req.iter_mut().find(|(k, _)| k == id) {
            Some((_, v)) => v.push(r),
            None => by_req.push((id.to_string(), vec![r])),
        }
    }
    let mut out: Vec<(String, Value)> = Vec::new();
    for (req_id, samples) in by_req {
        let last = samples[samples.len() - 1];
        let ts = last.get("ts").and_then(Value::as_str).unwrap_or("");
        match jsrt::date_parse(ts) {
            Some(ms) if now_ms - ms <= stale_ms => {}
            _ => continue,
        }
        let tokens = |r: &Value| {
            num(r, "responseTokens").unwrap_or(0.0) + num(r, "reasoningTokens").unwrap_or(0.0)
        };
        let tok_per_sec = (samples.len() >= 2)
            .then(|| {
                let prev = samples[samples.len() - 2];
                let delta_ms =
                    num(last, "elapsedMs").unwrap_or(0.0) - num(prev, "elapsedMs").unwrap_or(0.0);
                (delta_ms > 0.0).then(|| (tokens(last) - tokens(prev)) / delta_ms * 1000.0)
            })
            .flatten();
        out.push((
            ts.to_string(),
            json!({
                "reqId": req_id,
                "leg": str_or_null(last, "leg"),
                "modelResolved": str_or_null(last, "modelResolved"),
                "sessionId": str_or_null(last, "sessionId"),
                "phase": str_or_null(last, "phase"),
                "responseTokens": str_or_null(last, "responseTokens"),
                "reasoningTokens": str_or_null(last, "reasoningTokens"),
                "elapsedMs": str_or_null(last, "elapsedMs"),
                "lastSeenTs": ts,
                "tokPerSec": tok_per_sec.map_or(Value::Null, bd_domain::js::json_number),
            }),
        ));
    }
    out.sort_by(|a, b| jsrt::locale_compare(&b.0, &a.0));
    out.into_iter().map(|(_, v)| v).collect()
}

/// `buildRouterTelemetryResponse`.
pub fn build_router_telemetry_response(
    dir: Option<&str>,
    stale_ms: Option<f64>,
    now_ms: f64,
) -> Value {
    let window = DEFAULT_TAIL_WINDOW_BYTES;
    let (file, lines) = match tail_of(dir, select_router_telemetry_file, window, now_ms) {
        Ok(x) => x,
        Err(reply) => return reply,
    };
    let (requests, _) = parse_jsonl_lines(&lines, &ROUTER_REQUEST_RECORD);
    let (progress, _) = parse_jsonl_lines(&lines, &ROUTER_PROGRESS_RECORD);
    if requests.is_empty() && progress.is_empty() {
        return unavailable("empty");
    }
    let recent = requests[requests.len().saturating_sub(DEFAULT_MAX_RECENT)..].to_vec();
    let terminal = requests
        .iter()
        .filter_map(|r| r.get("reqId").and_then(Value::as_str).map(str::to_string))
        .collect();
    let in_flight = derive_in_flight(
        &progress,
        &terminal,
        now_ms,
        stale_ms.unwrap_or(DEFAULT_TELEMETRY_STALE_MS),
    );
    let legs = summarise_legs(&recent);
    json!({
        "available": true,
        "generatedAt": jsrt::iso_from_ms(now_ms as i64),
        "windowBytes": window,
        "file": file,
        "recent": recent,
        "inFlight": in_flight,
        "legs": serde_json::to_value(legs).unwrap_or(Value::Null),
    })
}

// ---- sidecarFeed.ts -----------------------------------------------------------------------

pub const SIDECAR_ERROR_KINDS: &[&str] = &["http-error", "child-stderr", "orphan-tool-result"];
pub const SIDECAR_MAX_RECENT_ERRORS: usize = 20;

/// `x ?? null` on a record field (`undefined`/`null` -> null).
fn field(r: &Value, k: &str) -> Value {
    r.get(k).cloned().unwrap_or(Value::Null)
}

/// `summarisePersonas`: latest state per persona, sorted by `persona.localeCompare`.
pub fn summarise_personas(records: &[Value]) -> Vec<Value> {
    let mut by: Vec<(String, Map<String, Value>)> = Vec::new();
    for r in records {
        let persona = r
            .get("persona")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let i = match by.iter().position(|(p, _)| *p == persona) {
            Some(i) => i,
            None => {
                let mut e = Map::new();
                e.insert("persona".into(), Value::String(persona.clone()));
                e.insert("threadId".into(), Value::Null);
                e.insert("model".into(), Value::Null);
                e.insert("lastSeenTs".into(), field(r, "ts"));
                e.insert("lastKind".into(), field(r, "kind"));
                by.push((persona, e));
                by.len() - 1
            }
        };
        let e = &mut by[i].1;
        for k in ["threadId", "model"] {
            if let Some(v) = r.get(k).filter(|v| !v.is_null()) {
                e.insert(k.into(), v.clone());
            }
        }
        e.insert("lastSeenTs".into(), field(r, "ts"));
        e.insert("lastKind".into(), field(r, "kind"));
    }
    by.sort_by(|a, b| jsrt::locale_compare(&a.0, &b.0));
    by.into_iter().map(|(_, e)| Value::Object(e)).collect()
}

/// `collectRecentErrors`: the newest `limit` error-ish lines, oldest first.
pub fn collect_recent_errors(records: &[Value], limit: usize) -> Vec<Value> {
    let errors: Vec<Value> = records
        .iter()
        .filter(|r| {
            r.get("kind")
                .and_then(Value::as_str)
                .is_some_and(|k| SIDECAR_ERROR_KINDS.contains(&k))
        })
        .map(|r| {
            let message = match r.get("message") {
                None | Some(Value::Null) => field(r, "text"),
                Some(m) => m.clone(),
            };
            json!({
                "ts": field(r, "ts"),
                "persona": field(r, "persona"),
                "kind": field(r, "kind"),
                "status": field(r, "status"),
                "message": message,
            })
        })
        .collect();
    errors[errors.len().saturating_sub(limit)..].to_vec()
}

/// `buildRouterSidecarResponse`.
pub fn build_router_sidecar_response(dir: Option<&str>, now_ms: f64) -> Value {
    let (file, lines) = match tail_of(dir, select_sidecar_file, DEFAULT_TAIL_WINDOW_BYTES, now_ms) {
        Ok(x) => x,
        Err(reply) => return reply,
    };
    let (records, _) = parse_jsonl_lines(&lines, &ROUTER_SIDECAR_RECORD);
    if records.is_empty() {
        return unavailable("empty");
    }
    let mut by_kind = Map::new();
    for r in &records {
        let k = r
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let n = by_kind.get(&k).and_then(Value::as_f64).unwrap_or(0.0) + 1.0;
        by_kind.insert(k, bd_domain::js::json_number(n));
    }
    json!({
        "available": true,
        "generatedAt": jsrt::iso_from_ms(now_ms as i64),
        "file": file,
        "byKind": by_kind,
        "personas": summarise_personas(&records),
        "recentErrors": collect_recent_errors(&records, SIDECAR_MAX_RECENT_ERRORS),
    })
}

#[cfg(test)]
#[path = "router_feeds_tests.rs"]
mod tests;
