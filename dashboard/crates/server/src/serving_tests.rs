//! Reader edge cases over the committed `fixtures/llama-config.yaml` and `GET /api/serving`
//! projection cases.
use super::*;

#[test]
fn tokenize_and_int_flags() {
    assert_eq!(tokenize_cmd("  a\n\tb\u{a0}c  "), ["a", "b", "c"]);
    assert_eq!(extract_recipe("x -ncmoe -1").moe_cpu_layers, None);
    assert_eq!(extract_recipe("x -ncmoe 007").moe_cpu_layers, Some(7.0));
    assert_eq!(extract_recipe("x -m").model_path, None);
}

// ---- GET /api/serving projection (`serving/llamaConfig.test.ts`) --------------------------

fn fixture_models() -> Vec<Value> {
    let path = crate::test_support::fixtures("llama-config.yaml");
    read_serving_config(&path).unwrap().models
}

fn model<'a>(models: &'a [Value], key: &str) -> &'a Value {
    models.iter().find(|m| m["modelKey"] == key).unwrap()
}

#[test]
fn serving_flags_from_macros_and_own_cmd() {
    let models = fixture_models();
    let embed = model(&models, "fixture-embed-f16");
    assert_eq!(embed["aliases"], serde_json::json!(["fixture-embed"]));
    assert_eq!(embed["flags"]["contextLength"], 8192.0);
    assert_eq!(embed["flags"]["gpuLayers"], 99.0);
    let all = embed["flags"]["allFlags"].as_array().unwrap();
    assert_eq!(
        all[0],
        serde_json::json!({"flag": "--host", "value": "0.0.0.0"})
    );
    assert_eq!(
        all.last().unwrap(),
        &serde_json::json!({"flag": "--embeddings", "value": null})
    );
    let chat = model(&models, "fixture-chat-q4");
    assert_eq!(chat["ttl"], 1800);
    assert_eq!(chat["checkEndpoint"], "/health");
    assert_eq!(chat["flags"]["kvCacheTypeK"], "q4_0");
    let edge = model(&models, "fixture-flags-edge");
    let flags: Vec<(String, Value)> = edge["flags"]["allFlags"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| (f["flag"].as_str().unwrap().to_string(), f["value"].clone()))
        .collect();
    assert_eq!(
        flags,
        vec![
            ("--host".into(), Value::from("0.0.0.0")),
            ("--port".into(), Value::from("${PORT}")),
            (
                "-m".into(),
                Value::from("/models/example-org/fixture-edge-gguf/fixture-edge.gguf")
            ),
            ("--jinja".into(), Value::Null),
            ("--temp".into(), Value::from("0.8")),
            ("--top-p".into(), Value::from("-1")),
            ("-ngl".into(), Value::from("99")),
        ]
    );
    assert_eq!(edge["capability"], Value::Null);
    assert!(!models.iter().any(|m| m["modelKey"] == "fixture-unaliased"));
    let keys: Vec<&String> = chat.as_object().unwrap().keys().collect();
    assert_eq!(
        keys,
        [
            "modelKey",
            "name",
            "aliases",
            "ttl",
            "checkEndpoint",
            "flags",
            "capability",
            "recipe"
        ]
    );
}

#[test]
fn serving_capability_nulls_and_passthrough() {
    let text = r#"
macros:
  a: "-c 4096"
  b: "${a} -ngl 1"
models:
  m:
    aliases: [m1]
    ttl: "300"
    cmd: x ${b} ${undefined_macro} -fa
    metadata:
      context_length: 4096
      tool_calling: false
      serving: { quant: Q4 }
      sampling: { temperature: 0.7 }
      sources: { quant: { url: none, reason: r } }
"#;
    let models = parse_served_aliases(text).unwrap();
    let m = &models[0];
    // One pass: `${a}` inside macro `b` is not re-expanded (macros never reference each other).
    assert_eq!(m["flags"]["contextLength"], Value::Null);
    // `${a}` survives (leading the flags, so not a flag); the unknown macro stays literal and,
    // following a value, is skipped as a non-flag token.
    assert_eq!(
        m["flags"]["allFlags"],
        serde_json::json!([{"flag": "-ngl", "value": "1"}, {"flag": "-fa", "value": null}])
    );
    assert_eq!(m["flags"]["flashAttention"], Value::Null);
    assert_eq!(m["ttl"], Value::Null);
    let c = &m["capability"];
    assert_eq!(c["toolCalling"], false);
    assert_eq!(c["inputModalities"], serde_json::json!([]));
    assert_eq!(c["serving"]["quant"], "Q4");
    assert_eq!(c["serving"]["ubatch"], Value::Null);
    assert_eq!(c["sampling"]["temperature"], 0.7);
    assert_eq!(c["sampling"]["topK"], Value::Null);
    assert_eq!(c["maxOutput"], Value::Null);
    assert_eq!(c["sources"]["quant"]["url"], "none");
    // `metadata: ~` and an empty document throw (the route's 502).
    assert!(parse_served_aliases("models:\n  m:\n    aliases: [a]\n    metadata: ~\n").is_err());
    assert!(parse_served_aliases("").is_err());
}
