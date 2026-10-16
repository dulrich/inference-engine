//! The drop-box write planes: `routes/control.ts` (`POST /api/control/*`, `control-request-v1`
//! files) and `routes/servingControl.ts` (`POST /api/serving/visibility`, `serving-request-v1`
//! files; `GET /api/serving/status`). Every write is collision-safe exclusive creation: a temp
//! file in the requests dir, `link(tmp, dst)` (EEXIST on an id collision -> a fresh id, bounded),
//! then `unlink(tmp)` — never a rename over an existing request. `accepted: true` means
//! *recorded*; the host dispatcher (Python) reads the bytes, so they are `JSON.stringify`'s.
use crate::jsrt;
use bd_domain::contract::{
    CONTROL_CANCEL_REQUEST, CONTROL_REQUEST_SCHEMA_V1, CONTROL_START_REQUEST,
    SERVING_REQUEST_SCHEMA_V1, SERVING_STATUS_FILE, SERVING_STATUS_STALE_MS,
    SERVING_VISIBILITY_REQUEST,
};
use bd_domain::validate;
use serde_json::{json, Map, Value};
use std::path::Path;

/// `MAX_ID_ATTEMPTS` (`control.ts:26`).
pub const MAX_ID_ATTEMPTS: usize = 5;

/// The 503 body text when `CONTROL_REQUESTS_DIR` is unset (both planes share it).
pub const UNCONFIGURED: &str = "control plane not configured: CONTROL_REQUESTS_DIR is unset";

pub const SERVING_REQUEST_ACCEPTED_MESSAGE: &str =
    "Visibility change recorded; the effect lands on the next dispatcher pass (about a minute).";
const STOP_MESSAGE: &str =
    "Stop delivered to the live run; effect is pause-after-current-cell, or a no-op if the run completes first.";
const CANCEL_MESSAGE: &str = "Cancellation is effective only before dispatch claim.";

/// `yyyymmddTHHMMSSZ` of the current UTC time.
fn utc_compact() -> String {
    let iso = jsrt::iso_from_ms(jsrt::now_ms() as i64);
    format!(
        "{}{}{}T{}{}{}Z",
        &iso[0..4],
        &iso[5..7],
        &iso[8..10],
        &iso[11..13],
        &iso[14..16],
        &iso[17..19]
    )
}

fn hex16() -> std::io::Result<String> {
    Ok(jsrt::random_uuid()?.replace('-', "")[..16].to_string())
}

/// `generateRequestId`: `req-<UTCcompact>-<16 hex>`.
pub fn generate_request_id() -> std::io::Result<String> {
    Ok(format!("req-{}-{}", utc_compact(), hex16()?))
}

/// `generateServingRequestId`: `srv-<UTCcompact>-<16 hex>`.
pub fn generate_serving_request_id() -> std::io::Result<String> {
    Ok(format!("srv-{}-{}", utc_compact(), hex16()?))
}

pub type IdFactory<'a> = &'a mut dyn FnMut() -> std::io::Result<String>;

/// `writeControlRequestExclusive`: returns the request id, or the thrown error's text (the
/// route's plain 500).
pub fn write_exclusive(
    requests_dir: &str,
    build_body: &dyn Fn(&str, &str) -> Value,
    id_factory: IdFactory,
) -> Result<String, String> {
    for _ in 0..MAX_ID_ATTEMPTS {
        let request_id = id_factory().map_err(|e| e.to_string())?;
        let requested_at = jsrt::iso_now();
        let body = build_body(&request_id, &requested_at);
        let dst = jsrt::path_join(requests_dir, &format!("{request_id}.json"));
        let uuid = jsrt::random_uuid().map_err(|e| e.to_string())?;
        let tmp = jsrt::path_join(requests_dir, &format!(".{request_id}.{uuid}.tmp"));
        std::fs::write(&tmp, jsrt::stringify(&body)).map_err(|e| format!("{e}, open '{tmp}'"))?;
        if let Err(e) = std::fs::hard_link(&tmp, &dst) {
            std::fs::remove_file(&tmp).map_err(|e| format!("{e}, unlink '{tmp}'"))?;
            if e.kind() == std::io::ErrorKind::AlreadyExists {
                continue;
            }
            return Err(format!("{e}, link '{tmp}' -> '{dst}'"));
        }
        std::fs::remove_file(&tmp).map_err(|e| format!("{e}, unlink '{tmp}'"))?;
        return Ok(request_id);
    }
    Err(format!(
        "control request id collided {MAX_ID_ATTEMPTS} times in a row"
    ))
}

/// A route result: status + JSON body, or a thrown error (plain 500).
#[derive(Debug)]
pub struct ActionResult {
    pub status: u16,
    pub body: Value,
}

fn unconfigured() -> ActionResult {
    ActionResult {
        status: 503,
        body: json!({ "error": UNCONFIGURED }),
    }
}

/// `if (!requestsDir)`: unset and empty are both "off".
fn configured(dir: Option<&str>) -> Option<&str> {
    dir.filter(|d| !d.is_empty())
}

/// `parsed.error.issues.map((issue) => issue.message).join("; ")`.
fn bad_request(issues: &[validate::Issue]) -> ActionResult {
    let text = issues
        .iter()
        .map(|i| i.message.as_str())
        .collect::<Vec<_>>()
        .join("; ");
    ActionResult {
        status: 400,
        body: json!({ "error": text }),
    }
}

fn accepted(request_id: String, message: Option<&str>) -> ActionResult {
    let mut body = Map::new();
    body.insert("requestId".into(), Value::String(request_id));
    body.insert("accepted".into(), Value::Bool(true));
    if let Some(m) = message {
        body.insert("message".into(), Value::String(m.into()));
    }
    ActionResult {
        status: 200,
        body: Value::Object(body),
    }
}

/// `recordSimpleAction`: `arm` / `disarm` / `stop`.
pub fn record_simple_action(
    requests_dir: Option<&str>,
    action: &str,
    id_factory: IdFactory,
) -> Result<ActionResult, String> {
    let Some(dir) = configured(requests_dir) else {
        return Ok(unconfigured());
    };
    let request_id = write_exclusive(
        dir,
        &|id, at| {
            json!({
                "schema": CONTROL_REQUEST_SCHEMA_V1,
                "request_id": id,
                "action": action,
                "requested_at": at,
            })
        },
        id_factory,
    )?;
    Ok(accepted(
        request_id,
        (action == "stop").then_some(STOP_MESSAGE),
    ))
}

/// `recordStartAction`: `{queueStem, minutes}`.
pub fn record_start_action(
    requests_dir: Option<&str>,
    body: &Value,
    id_factory: IdFactory,
) -> Result<ActionResult, String> {
    let Some(dir) = configured(requests_dir) else {
        return Ok(unconfigured());
    };
    let parsed = match validate::parse(&CONTROL_START_REQUEST.node, body) {
        Ok(v) => v,
        Err(issues) => return Ok(bad_request(&issues)),
    };
    let request_id = write_exclusive(
        dir,
        &|id, at| {
            json!({
                "schema": CONTROL_REQUEST_SCHEMA_V1,
                "request_id": id,
                "action": "start",
                "queue_stem": parsed["queueStem"],
                "minutes": parsed["minutes"],
                "requested_at": at,
            })
        },
        id_factory,
    )?;
    Ok(accepted(request_id, None))
}

/// `recordCancelAction`: `{targetRequestId}`.
pub fn record_cancel_action(
    requests_dir: Option<&str>,
    body: &Value,
    id_factory: IdFactory,
) -> Result<ActionResult, String> {
    let Some(dir) = configured(requests_dir) else {
        return Ok(unconfigured());
    };
    let parsed = match validate::parse(&CONTROL_CANCEL_REQUEST.node, body) {
        Ok(v) => v,
        Err(issues) => return Ok(bad_request(&issues)),
    };
    let request_id = write_exclusive(
        dir,
        &|id, at| {
            json!({
                "schema": CONTROL_REQUEST_SCHEMA_V1,
                "request_id": id,
                "action": "cancel",
                "target_request_id": parsed["targetRequestId"],
                "requested_at": at,
            })
        },
        id_factory,
    )?;
    Ok(accepted(request_id, Some(CANCEL_MESSAGE)))
}

/// `recordServingVisibilityAction`: `{modelKey, consumers}` (the full desired array).
pub fn record_serving_visibility_action(
    requests_dir: Option<&str>,
    body: &Value,
    id_factory: IdFactory,
) -> Result<ActionResult, String> {
    let Some(dir) = configured(requests_dir) else {
        return Ok(unconfigured());
    };
    let parsed = match validate::parse(&SERVING_VISIBILITY_REQUEST.node, body) {
        Ok(v) => v,
        Err(issues) => return Ok(bad_request(&issues)),
    };
    let request_id = write_exclusive(
        dir,
        &|id, at| {
            json!({
                "schema": SERVING_REQUEST_SCHEMA_V1,
                "request_id": id,
                "action": "set-visibility",
                "model_key": parsed["modelKey"],
                "consumers": parsed["consumers"],
                "requested_at": at,
            })
        },
        id_factory,
    )?;
    Ok(accepted(request_id, Some(SERVING_REQUEST_ACCEPTED_MESSAGE)))
}

/// `SERVING_STATUS_BASENAME`.
pub const SERVING_STATUS_BASENAME: &str = "serving-status.json";

fn unavailable(reason: &str) -> Value {
    json!({ "available": false, "reason": reason })
}

/// `readServingStatus(hostDir, now)`: always a 200 body.
pub fn read_serving_status(host_dir: Option<&str>, now_ms: f64) -> Value {
    let Some(dir) = configured(host_dir) else {
        return unavailable("disabled");
    };
    let raw = match std::fs::read(Path::new(&jsrt::path_join(dir, SERVING_STATUS_BASENAME))) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(_) => return unavailable("unreadable"),
    };
    if bd_domain::js::trim(&raw).is_empty() {
        return unavailable("empty");
    }
    let Ok(parsed) = jsrt::json_parse(&raw) else {
        return unavailable("unreadable");
    };
    let Ok(Value::Object(data)) = validate::parse(&SERVING_STATUS_FILE.node, &parsed) else {
        return unavailable("unreadable");
    };
    let Some(generated) = data
        .get("generated_at")
        .and_then(Value::as_str)
        .and_then(jsrt::date_parse)
    else {
        return unavailable("unreadable");
    };
    if now_ms - generated > SERVING_STATUS_STALE_MS as f64 {
        return unavailable("stale");
    }
    let mut out = Map::new();
    out.insert("available".into(), Value::Bool(true));
    out.extend(data);
    Value::Object(out)
}

/// `readJsonBody`: `request.json()`, `{}` when it throws.
pub fn read_json_body(bytes: &[u8]) -> Value {
    jsrt::json_parse(&String::from_utf8_lossy(bytes)).unwrap_or_else(|_| json!({}))
}

#[cfg(test)]
#[path = "control_tests.rs"]
mod tests;
