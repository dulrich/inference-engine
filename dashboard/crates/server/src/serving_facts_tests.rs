//! The facts states over a synthetic file built in-test (root rule 6: no live state pinned).
use super::*;
use crate::test_support::TempDir;

const CONFIG: &str = "models:\n  m:\n    cmd: llama-server -m /models/m.gguf\n";

pub(crate) fn facts_json(revision: &str) -> Value {
    json!({
        "schema": "serving-facts-v1",
        "generatedAt": "2026-10-25T12:00:00Z",
        "servingConfigRevision": revision,
        "timing": [{
            "modelKey": "m", "aliases": ["m-a"], "p50PrefillTokS": 100.5, "p90PrefillTokS": null,
            "p50DecodeTokS": 20.0, "p90DecodeTokS": null, "samplingArm": null,
            "sampleCount": 3, "mtp": false
        }],
        "measured": [{"alias": "m-a", "experimentId": "exp-1"}],
        "futureField": {"ignored": true}
    })
}

fn setup(facts: Option<&str>) -> (TempDir, String, String) {
    let t = TempDir::new("facts");
    t.write("config.yaml", CONFIG);
    if let Some(text) = facts {
        t.write("facts.json", text);
    }
    let cfg = format!("{}/config.yaml", t.0);
    let path = format!("{}/facts.json", t.0);
    (t, cfg, path)
}

#[test]
fn present_file_serves_both_lists_and_strips_unknown_fields() {
    let good = facts_json(&sha256_hex(CONFIG.as_bytes())).to_string();
    let (_t, cfg, path) = setup(Some(&good));
    let f = read_serving_facts(Some(&path), &cfg);
    let ok = f.as_ref().unwrap();
    assert_eq!(ok.generated_at, "2026-10-25T12:00:00Z");
    let body = response(&f, 4816);
    assert_eq!(body["available"], true);
    assert_eq!(body["timing"][0]["modelKey"], "m");
    assert_eq!(
        body["measured"],
        json!([{"alias": "m-a", "experimentId": "exp-1"}])
    );
    assert_eq!(body["researchPort"], 4816);
    assert!(body.get("futureField").is_none());
}

#[test]
fn unavailable_states() {
    let rev = sha256_hex(CONFIG.as_bytes());
    let (_t, cfg, path) = setup(None);
    assert_eq!(read_serving_facts(None, &cfg), Err(Unavailable::Disabled));
    assert_eq!(
        read_serving_facts(Some(""), &cfg),
        Err(Unavailable::Disabled)
    );
    assert_eq!(
        read_serving_facts(Some(&path), &cfg),
        Err(Unavailable::Absent)
    );
    let mut wrong_schema = facts_json(&rev);
    wrong_schema["schema"] = json!("serving-facts-v2");
    let mut missing = facts_json(&rev);
    missing.as_object_mut().unwrap().remove("measured");
    let mut bad_row = facts_json(&rev);
    bad_row["timing"][0]["sampleCount"] = json!("3");
    let mut bad_date = facts_json(&rev);
    bad_date["generatedAt"] = json!("yesterday");
    for text in [
        "not json".to_string(),
        "[]".to_string(),
        wrong_schema.to_string(),
        missing.to_string(),
        bad_row.to_string(),
        bad_date.to_string(),
    ] {
        let (_t, cfg, path) = setup(Some(&text));
        let f = read_serving_facts(Some(&path), &cfg);
        assert_eq!(f, Err(Unavailable::Invalid), "{text}");
        assert_eq!(
            response(&f, 4816),
            json!({"available": false, "reason": "invalid", "researchPort": 4816})
        );
    }
    let stale = facts_json(&sha256_hex(b"other config")).to_string();
    let (_t, cfg, path) = setup(Some(&stale));
    let f = read_serving_facts(Some(&path), &cfg);
    assert_eq!(f, Err(Unavailable::StaleConfig));
    assert_eq!(response(&f, 1)["reason"], "stale-config");
    assert_eq!(
        read_serving_facts(Some(&path), "/absent/config.yaml"),
        Err(Unavailable::StaleConfig)
    );
    let (t, cfg, _) = setup(None);
    assert_eq!(
        read_serving_facts(Some(&t.0), &cfg),
        Err(Unavailable::Unreadable)
    );
}

#[test]
fn sha256_is_lowercase_hex() {
    assert_eq!(
        sha256_hex(b"abc"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
}
