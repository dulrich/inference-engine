//! `routes/control.test.ts` (id-collision retry/exhaustion, `:150-190`) and
//! `routes/servingControl.test.ts` (idFactory collision, the 180 s status boundary, the
//! unavailable verdicts) — the cases WP-1 could not reach black-box.
use super::*;
use crate::record::tests::{fixtures, TempDir};
use serde_json::json;

fn ids(list: Vec<&'static str>) -> impl FnMut() -> std::io::Result<String> {
    let mut it = list.into_iter();
    move || Ok(it.next().expect("factory called too often").to_string())
}

fn read(dir: &str, name: &str) -> String {
    std::fs::read_to_string(jsrt::path_join(dir, name)).unwrap()
}

fn entries(dir: &str) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    v.sort();
    v
}

#[test]
fn collision_regenerates_and_never_overwrites() {
    let t = TempDir::new("ctl-collision");
    let colliding = "req-20260823T000000Z-aaaaaaaaaaaaaaaa";
    let fresh = "req-20260823T000001Z-bbbbbbbbbbbbbbbb";
    let sentinel = json!({"schema": "control-request-v1", "request_id": colliding, "action": "arm", "requested_at": "sentinel"}).to_string();
    t.write(&format!("{colliding}.json"), &sentinel);
    let mut calls = 0;
    let mut inner = ids(vec![colliding, fresh]);
    let mut factory = || {
        calls += 1;
        inner()
    };
    let r = record_simple_action(Some(&t.0), "arm", &mut factory).unwrap();
    assert_eq!(calls, 2);
    assert_eq!(r.status, 200);
    assert_eq!(r.body["requestId"], fresh);
    assert_eq!(read(&t.0, &format!("{colliding}.json")), sentinel);
    let file: Value = serde_json::from_str(&read(&t.0, &format!("{fresh}.json"))).unwrap();
    assert_eq!(file["request_id"], fresh);
    // Temp files unlinked: exactly the two request files remain.
    assert_eq!(
        entries(&t.0),
        vec![format!("{colliding}.json"), format!("{fresh}.json")]
    );
}

#[test]
fn exhaustion_throws_after_max_attempts() {
    let t = TempDir::new("ctl-exhausted");
    let id = "req-20260823T000000Z-cccccccccccccccc";
    t.write(&format!("{id}.json"), r#"{"sentinel":true}"#);
    let mut calls = 0;
    let mut factory = || {
        calls += 1;
        Ok(id.to_string())
    };
    let err = write_exclusive(
        &t.0,
        &|i, at| json!({"request_id": i, "requested_at": at}),
        &mut factory,
    )
    .unwrap_err();
    assert_eq!(calls, MAX_ID_ATTEMPTS);
    assert_eq!(err, "control request id collided 5 times in a row");
    assert_eq!(read(&t.0, &format!("{id}.json")), r#"{"sentinel":true}"#);
    assert_eq!(entries(&t.0), vec![format!("{id}.json")]);
}

#[test]
fn serving_id_factory_collision_and_bytes() {
    let t = TempDir::new("srv-collision");
    let a = "srv-20260913T120000Z-aaaaaaaaaaaaaaaa";
    let b = "srv-20260913T120001Z-bbbbbbbbbbbbbbbb";
    let mut factory = ids(vec![a, a, b]);
    let first = record_serving_visibility_action(
        Some(&t.0),
        &json!({"modelKey": "a", "consumers": []}),
        &mut factory,
    )
    .unwrap();
    let second = record_serving_visibility_action(
        Some(&t.0),
        &json!({"modelKey": "b", "consumers": ["openwebui"], "extra": 1}),
        &mut factory,
    )
    .unwrap();
    assert_eq!(first.body["requestId"], a);
    assert_eq!(second.body["requestId"], b);
    assert_eq!(second.body["message"], SERVING_REQUEST_ACCEPTED_MESSAGE);
    let text = read(&t.0, &format!("{b}.json"));
    let at: Value = serde_json::from_str(&text).unwrap();
    let at = at["requested_at"].as_str().unwrap().to_string();
    assert_eq!(
        text,
        format!(
            r#"{{"schema":"serving-request-v1","request_id":"{b}","action":"set-visibility","model_key":"b","consumers":["openwebui"],"requested_at":"{at}"}}"#
        )
    );
}

#[test]
fn start_and_cancel_bytes_and_messages() {
    let t = TempDir::new("ctl-bytes");
    let id = "req-20260827T120000Z-00000000000000a1";
    let r = record_start_action(
        Some(&t.0),
        &json!({"queueStem": "q-1", "minutes": 480, "extra": true}),
        &mut ids(vec![id]),
    )
    .unwrap();
    assert_eq!(r.body, json!({"requestId": id, "accepted": true}));
    let text = read(&t.0, &format!("{id}.json"));
    assert!(text.starts_with(&format!(
        r#"{{"schema":"control-request-v1","request_id":"{id}","action":"start","queue_stem":"q-1","minutes":480,"requested_at":""#
    )));
    let c = record_cancel_action(
        Some(&t.0),
        &json!({"targetRequestId": id}),
        &mut ids(vec!["req-20260827T120001Z-00000000000000a2"]),
    )
    .unwrap();
    assert_eq!(c.body["message"], CANCEL_MESSAGE);
    let s = record_simple_action(
        Some(&t.0),
        "stop",
        &mut ids(vec!["req-20260827T120002Z-00000000000000a3"]),
    )
    .unwrap();
    assert_eq!(s.body["message"], STOP_MESSAGE);
}

#[test]
fn bad_requests_write_nothing_and_name_the_domain_messages() {
    let t = TempDir::new("ctl-400");
    let mut never = || -> std::io::Result<String> { panic!("no write on a 400") };
    let r = record_start_action(
        Some(&t.0),
        &json!({"queueStem": "Bad_Stem", "minutes": 60}),
        &mut never,
    )
    .unwrap();
    assert_eq!(r.status, 400);
    assert!(r.body["error"]
        .as_str()
        .unwrap()
        .contains("queueStem must match ^[a-z0-9][a-z0-9-]*$"));
    let r = record_cancel_action(
        Some(&t.0),
        &json!({"targetRequestId": "nightly-20260827"}),
        &mut never,
    )
    .unwrap();
    assert!(r.body["error"]
        .as_str()
        .unwrap()
        .contains("targetRequestId must match the request-id grammar"));
    let r = record_serving_visibility_action(
        Some(&t.0),
        &json!({"modelKey": "", "consumers": []}),
        &mut never,
    )
    .unwrap();
    assert!(r.body["error"]
        .as_str()
        .unwrap()
        .contains("modelKey must be a non-empty llama/config.yaml model key"));
    for minutes in [json!(14), json!(481), json!(60.5), json!("60")] {
        let r = record_start_action(
            Some(&t.0),
            &json!({"queueStem": "q", "minutes": minutes}),
            &mut never,
        )
        .unwrap();
        assert_eq!(r.status, 400);
        assert!(!r.body["error"].as_str().unwrap().is_empty());
    }
    assert!(entries(&t.0).is_empty());
}

#[test]
fn unconfigured_is_503_for_unset_and_empty() {
    let mut never = || -> std::io::Result<String> { panic!("no write") };
    for dir in [None, Some("")] {
        let r = record_simple_action(dir, "arm", &mut never).unwrap();
        assert_eq!(r.status, 503);
        assert_eq!(r.body, json!({"error": UNCONFIGURED}));
        let r = record_serving_visibility_action(dir, &json!({}), &mut never).unwrap();
        assert_eq!(r.status, 503);
    }
}

#[test]
fn id_grammar() {
    let re = |s: &str, p: &str| {
        s.len() == 37
            && s.starts_with(p)
            && s.as_bytes()[12] == b'T'
            && s.as_bytes()[19] == b'Z'
            && s[21..]
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    };
    assert!(re(&generate_request_id().unwrap(), "req-"));
    assert!(re(&generate_serving_request_id().unwrap(), "srv-"));
}

const GENERATED_AT_MS: f64 = 1_789_300_800_000.0; // 2026-09-13T12:00:00.000Z

fn host() -> String {
    fixtures("serving/contract-v1/control/host")
}

#[test]
fn serving_status_180s_boundary() {
    assert_eq!(
        jsrt::date_parse("2026-09-13T12:00:00.000Z"),
        Some(GENERATED_AT_MS)
    );
    let ok = read_serving_status(Some(&host()), GENERATED_AT_MS + 179_000.0);
    assert_eq!(ok["available"], true);
    assert_eq!(ok.as_object().unwrap().keys().next().unwrap(), "available");
    assert_eq!(ok["schema"], "serving-status-v1");
    assert_eq!(
        read_serving_status(Some(&host()), GENERATED_AT_MS + 180_000.0)["available"],
        true
    );
    assert_eq!(
        read_serving_status(Some(&host()), GENERATED_AT_MS + 181_000.0),
        json!({"available": false, "reason": "stale"})
    );
}

#[test]
fn serving_status_unavailable_verdicts() {
    let un = |r: &str| json!({"available": false, "reason": r});
    assert_eq!(read_serving_status(None, 0.0), un("disabled"));
    assert_eq!(read_serving_status(Some(""), 0.0), un("disabled"));
    let t = TempDir::new("srv-status");
    assert_eq!(read_serving_status(Some(&t.0), 0.0), un("unreadable"));
    for (text, reason) in [
        (" \n", "empty"),
        ("{", "unreadable"),
        (
            r#"{"schema":"serving-status-v0","generated_at":"2026-09-13T12:00:00Z"}"#,
            "unreadable",
        ),
    ] {
        t.write(SERVING_STATUS_BASENAME, text);
        assert_eq!(read_serving_status(Some(&t.0), 0.0), un(reason), "{text}");
    }
    let mut good: Value = serde_json::from_str(&read(&host(), SERVING_STATUS_BASENAME)).unwrap();
    good["generated_at"] = json!("not a date");
    t.write(SERVING_STATUS_BASENAME, &good.to_string());
    assert_eq!(read_serving_status(Some(&t.0), 0.0), un("unreadable"));
}

#[test]
fn json_body_fallback() {
    assert_eq!(read_json_body(b""), json!({}));
    assert_eq!(read_json_body(b"{"), json!({}));
    assert_eq!(read_json_body(b"\"not json\""), json!("not json"));
}
