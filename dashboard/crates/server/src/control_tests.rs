//! `routes/control.test.ts` (id-collision retry/exhaustion, `:150-190`) and
//! `routes/servingControl.test.ts` (idFactory collision, the 180 s status boundary, the
//! unavailable verdicts) — the cases WP-1 could not reach black-box.
use super::*;
use crate::test_support::{fixtures, TempDir};
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
    let r = record_serving_visibility_action(
        Some(&t.0),
        &json!({"modelKey": "a", "consumers": []}),
        &mut factory,
    )
    .unwrap();
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
fn bad_requests_write_nothing_and_name_the_domain_messages() {
    let t = TempDir::new("ctl-400");
    let mut never = || -> std::io::Result<String> { panic!("no write on a 400") };
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
    assert_eq!(r.status, 400);
    assert!(entries(&t.0).is_empty());
}

#[test]
fn unconfigured_is_503_for_unset_and_empty() {
    let mut never = || -> std::io::Result<String> { panic!("no write") };
    for dir in [None, Some("")] {
        let r = record_serving_visibility_action(dir, &json!({}), &mut never).unwrap();
        assert_eq!(r.status, 503);
        assert_eq!(r.body, json!({"error": UNCONFIGURED}));
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

// ---- serving-request-v2: the catalog intents (plans/serving-control-redesign.md WP-4) ------
// The same committed bytes `control/tests/test_catalog_intents.py` reads.

fn v2(rel: &str) -> String {
    fixtures(&format!("serving/contract-v2/{rel}"))
}

fn v2_json(rel: &str) -> Value {
    serde_json::from_str(&std::fs::read_to_string(v2(rel)).unwrap()).unwrap()
}

/// The API body that produces each committed v2 request fixture.
fn v2_api_cases() -> Vec<(&'static str, &'static str, Value)> {
    vec![
        (
            "srv-20261019T120001Z-00000000000000b1",
            "catalog.add",
            json!({"provider": "openrouter", "id": "meta-llama/llama-3.2-1b-instruct"}),
        ),
        (
            "srv-20261019T120002Z-00000000000000b2",
            "catalog.remove",
            json!({"provider": "or-anthropic", "id": "z-ai/glm-5.3-flash"}),
        ),
        (
            "srv-20261019T120003Z-00000000000000b3",
            "catalog.consumers",
            json!({"provider": "codex-a", "id": "gpt-6-sol", "consumers": []}),
        ),
        (
            "srv-20261019T120004Z-00000000000000b4",
            "catalog.consumers",
            json!({"modelKey": "bge-m3-f16", "consumers": ["openwebui"]}),
        ),
        (
            "srv-20261019T120005Z-00000000000000b5",
            "catalog.add",
            json!({"provider": "claude-local", "id": "gemma-4-12b-qat", "consumers": ["openwebui"]}),
        ),
    ]
}

#[test]
fn catalog_intents_write_the_committed_v2_fixture_bytes() {
    for (rid, action, body) in v2_api_cases() {
        let t = TempDir::new("catalog-v2");
        let mut factory = ids(vec![rid]);
        let r = record_catalog_action(action, Some(&t.0), &body, &mut factory).unwrap();
        assert_eq!(r.status, 200, "{rid}");
        assert_eq!(r.body["requestId"], rid);
        assert_eq!(r.body["message"], SERVING_CATALOG_ACCEPTED_MESSAGE);
        let mut written: Value = serde_json::from_str(&read(&t.0, &format!("{rid}.json"))).unwrap();
        let mut fixture = v2_json(&format!("control/requests/{rid}.json"));
        assert!(
            validate::parse(&bd_domain::contract::SERVING_CATALOG_REQUEST.node, &fixture).is_ok(),
            "{rid} parses as ServingCatalogRequest"
        );
        written["requested_at"] = json!("");
        fixture["requested_at"] = json!("");
        assert_eq!(written, fixture, "{rid}");
        // Key order is the dispatcher's wire order, too.
        let keys = |v: &Value| v.as_object().unwrap().keys().cloned().collect::<Vec<_>>();
        assert_eq!(keys(&written), keys(&fixture), "{rid}");
    }
}

#[test]
fn catalog_intents_refuse_bad_shapes_and_write_nothing() {
    let t = TempDir::new("catalog-bad");
    let mut never = || -> std::io::Result<String> { panic!("no id for a refused body") };
    for (action, body) in [
        (
            "catalog.add",
            json!({"provider": "openrouter", "id": " spaced"}),
        ),
        (
            "catalog.add",
            json!({"provider": "Bad Provider", "id": "a/b"}),
        ),
        (
            "catalog.add",
            json!({"provider": "openrouter", "id": "a/b", "consumers": ["router"]}),
        ),
        ("catalog.remove", json!({"provider": "openrouter"})),
        (
            "catalog.consumers",
            json!({"provider": "codex-a", "id": "gpt-6-sol"}),
        ),
        (
            "catalog.consumers",
            json!({"provider": "codex-a", "id": "gpt-6-sol", "modelKey": "k", "consumers": []}),
        ),
        (
            "catalog.consumers",
            json!({"provider": "codex-a", "consumers": []}),
        ),
        ("catalog.consumers", json!({"consumers": []})),
    ] {
        let r = record_catalog_action(action, Some(&t.0), &body, &mut never).unwrap();
        assert_eq!(r.status, 400, "{action} {body}");
        assert!(r.body["error"].as_str().is_some_and(|e| !e.is_empty()));
    }
    assert!(entries(&t.0).is_empty());
    let mut never = || -> std::io::Result<String> { panic!("unconfigured") };
    let r = record_catalog_action("catalog.add", None, &json!({}), &mut never).unwrap();
    assert_eq!(r.status, 503);
}

#[test]
fn invalid_v2_fixtures_fail_the_wire_shape() {
    for name in [
        "request-v2-action-unknown.json",
        "request-v2-id-grammar.json",
        "request-v2-consumer-router.json",
    ] {
        let body = v2_json(&format!("invalid/{name}"));
        assert!(
            validate::parse(&bd_domain::contract::SERVING_CATALOG_REQUEST.node, &body).is_err(),
            "{name}"
        );
    }
}

#[test]
fn the_v2_status_fixture_reads_with_its_catalog_block_and_revisions() {
    let at = jsrt::date_parse("2026-10-19T12:01:00Z").unwrap();
    let ok = read_serving_status(Some(&v2("control/host")), at + 1_000.0);
    assert_eq!(ok["available"], true);
    let fixture = v2_json("control/host/serving-status.json");
    assert_eq!(ok["catalog"], fixture["catalog"]);
    assert_eq!(ok["recent"][0]["revision"], fixture["catalog"]["revision"]);
    // v1 records stay exactly as they were: no `revision` key, no `catalog` block.
    let v1 = read_serving_status(Some(&host()), GENERATED_AT_MS + 1_000.0);
    assert!(v1["recent"][0].get("revision").is_none());
    assert!(v1.get("catalog").is_none());
}
