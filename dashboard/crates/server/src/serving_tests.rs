//! `apps/api/src/serving/recipe.test.ts` resolver cases and `servingTiming*.test.ts` recipe-join
//! edges, over synthetic configs and the committed `apps/api/fixtures/llama-config.yaml`.
use super::*;

const CONFIG: &str = r#"
macros:
  common: "-ngl 99 -fa on"
models:
  plain:
    aliases: [plain-a, plain-b]
    cmd: /bin/llama-server -m /models/plain.gguf ${common} -c 8192
  mtp:
    aliases: [mtp-a]
    cmd: |
      /bin/llama-server -m /models/mtp.gguf
      --spec-type mtp -ctk q4_0 -ctv q4_0 -ncmoe 32
  ling-3:
    aliases: [ling-3-tiny-q8]
    cmd: /bin/llama-server -m /models/ling.gguf
  gemma:
    aliases: [gemma-4-12b]
    cmd: /bin/llama-server -m /models/gemma.gguf -md /models/draft.gguf
  nopath:
    aliases: [nopath-a]
    cmd: /bin/llama-server --port 1
  hidden:
    cmd: /bin/llama-server -m /models/hidden.gguf
"#;

fn served() -> Vec<ServedModel> {
    parse_served_models(CONFIG).unwrap()
}

fn row(path: Option<&str>, flags: Option<&str>) -> ResolvableTimingRow {
    ResolvableTimingRow {
        model_host_path: path.map(str::to_string),
        sweep_alias: None,
        schema_origin: "v2".into(),
        sweep_server_flags: flags.map(str::to_string),
    }
}

fn v1(alias: &str) -> ResolvableTimingRow {
    ResolvableTimingRow {
        model_host_path: None,
        sweep_alias: Some(alias.into()),
        schema_origin: "v1-timing".into(),
        sweep_server_flags: None,
    }
}

#[test]
fn parses_models_in_order_with_macro_expansion() {
    let s = served();
    let keys: Vec<&str> = s.iter().map(|m| m.model_key.as_str()).collect();
    assert_eq!(keys, ["plain", "mtp", "ling-3", "gemma", "nopath"]);
    assert!(s[0].cmd.contains("-ngl 99 -fa on"));
    assert_eq!(
        s[1].recipe,
        ServingRecipe {
            model_path: Some("/models/mtp.gguf".into()),
            draft_path: None,
            spec_type: Some("mtp".into()),
            moe_cpu_layers: Some(32.0),
            kv_k: "q4_0".into(),
            kv_v: "q4_0".into(),
        }
    );
    assert_eq!(s[4].recipe.model_path, None);
    assert!(parse_served_models("").is_err());
    assert!(parse_served_models("a: [").is_err());
    assert_eq!(parse_served_models("models: 3").unwrap(), vec![]);
}

#[test]
fn committed_fixture_config_parses() {
    let path = format!(
        "{}/../../apps/api/fixtures/llama-config.yaml",
        env!("CARGO_MANIFEST_DIR")
    );
    let models = read_served_models(&path).unwrap();
    assert!(!models.is_empty());
    assert!(read_served_models("/no/such.yaml")
        .unwrap_err()
        .starts_with("ENOENT: no such file or directory, open '/no/such.yaml'"));
}

#[test]
fn normalize_model_path_rewrites_only_the_stale_prefix() {
    assert_eq!(
        normalize_model_path("/srv/models/a/b.gguf"),
        "/models/a/b.gguf"
    );
    assert_eq!(normalize_model_path("/models/a.gguf"), "/models/a.gguf");
    assert_eq!(
        normalize_model_path("/elsewhere/a.gguf"),
        "/elsewhere/a.gguf"
    );
}

#[test]
fn server_flags_decode_and_projection() {
    assert_eq!(decode_server_flags(None), Ok(None));
    assert!(decode_server_flags(Some("{broken")).is_err());
    for non_object in ["[1,2]", "\"x\"", "7", "null"] {
        assert!(
            decode_server_flags(Some(non_object)).is_err(),
            "{non_object}"
        );
    }
    let defaults = recipe_from_server_flags(None).unwrap();
    assert_eq!(
        (defaults.mtp, defaults.kv_k.as_str(), defaults.kv_v.as_str()),
        (false, "f16", "f16")
    );
    let flags = |s: &str| decode_server_flags(Some(s)).unwrap();
    let r = recipe_from_server_flags(flags(r#"{"--spec-type":"mtp"}"#).as_ref()).unwrap();
    assert_eq!((r.mtp, r.spec_type.as_deref()), (true, Some("mtp")));
    assert!(
        recipe_from_server_flags(flags(r#"{"-md":"/d.gguf"}"#).as_ref())
            .unwrap()
            .mtp
    );
    for bad in [
        r#"{"-ctk":7}"#,
        r#"{"--spec-type":true}"#,
        r#"{"-ncmoe":"3"}"#,
        r#"{"-ncmoe":3.5}"#,
        r#"{"-ctv":null}"#,
    ] {
        assert_eq!(recipe_from_server_flags(flags(bad).as_ref()), None, "{bad}");
    }
    assert_eq!(
        recipe_from_server_flags(flags(r#"{"-ncmoe":32,"-c":8192,"-fa":"on"}"#).as_ref())
            .unwrap()
            .moe_cpu_layers,
        Some(32.0)
    );
}

#[test]
fn path_join_resolution() {
    let s = served();
    let plain =
        resolve_served_model(&row(Some("/srv/models/plain.gguf"), None), &s).unwrap();
    assert_eq!(plain.model_key, "plain");
    assert_eq!(plain.aliases, ["plain-a", "plain-b"]);
    assert!(plain.recipe_match);
    // '{}' is the null column's all-defaults recipe.
    assert!(
        resolve_served_model(&row(Some("/models/plain.gguf"), Some("{}")), &s)
            .unwrap()
            .recipe_match
    );
    // Off-recipe flags: resolved with recipeMatch false.
    let off = resolve_served_model(
        &row(Some("/models/plain.gguf"), Some(r#"{"-ctk":"q4_0"}"#)),
        &s,
    )
    .unwrap();
    assert!(!off.recipe_match);
    let mtp = resolve_served_model(
        &row(
            Some("/models/mtp.gguf"),
            Some(r#"{"--spec-type":"mtp","-ctk":"q4_0","-ctv":"q4_0","-ncmoe":32}"#),
        ),
        &s,
    )
    .unwrap();
    assert_eq!(
        (
            mtp.recipe_match,
            mtp.mtp,
            mtp.moe_cpu_layers,
            mtp.spec_type.as_deref()
        ),
        (true, true, Some(32.0), Some("mtp"))
    );
    let draft = resolve_served_model(
        &row(
            Some("/models/gemma.gguf"),
            Some(r#"{"-md":"/models/draft.gguf"}"#),
        ),
        &s,
    )
    .unwrap();
    assert!(draft.recipe_match && draft.mtp);
    // Unknown path, invalid evidence, wrong-typed key: unresolved.
    assert_eq!(
        resolve_served_model(&row(Some("/models/minicpm.gguf"), None), &s),
        None
    );
    assert_eq!(
        resolve_served_model(&row(Some("/models/plain.gguf"), Some("{broken")), &s),
        None
    );
    assert_eq!(
        resolve_served_model(&row(Some("/models/plain.gguf"), Some("[1,2]")), &s),
        None
    );
    assert_eq!(
        resolve_served_model(&row(Some("/models/plain.gguf"), Some(r#"{"-ctk":7}"#)), &s),
        None
    );
    // A v2 row with neither a path nor v1-timing provenance joins nothing.
    assert_eq!(resolve_served_model(&row(None, None), &s), None);
    // An alias-less model is never served; a model without -m is never path-joined.
    assert_eq!(
        resolve_served_model(&row(Some("/models/hidden.gguf"), None), &s),
        None
    );
}

#[test]
fn v1_timing_alias_join() {
    let s = served();
    let ling = resolve_served_model(&v1("ling-3-tiny-q8-full"), &s).unwrap();
    assert_eq!(
        ling,
        ResolvedServedModel {
            model_key: "ling-3".into(),
            aliases: vec!["ling-3-tiny-q8".into()],
            recipe_match: true,
            mtp: false,
            moe_cpu_layers: None,
            spec_type: None,
        }
    );
    assert!(resolve_served_model(&v1("plain-b"), &s).is_some());
    // Served recipe not the v1 baseline (MTP draft): excluded outright.
    assert_eq!(resolve_served_model(&v1("gemma-4-12b-full"), &s), None);
    // Served model without a recipe path: never the baseline.
    assert_eq!(resolve_served_model(&v1("nopath-a"), &s), None);
    assert_eq!(resolve_served_model(&v1("unknown"), &s), None);
    let mut no_alias = v1("x");
    no_alias.sweep_alias = None;
    assert_eq!(resolve_served_model(&no_alias, &s), None);
}

#[test]
fn tokenize_and_int_flags() {
    assert_eq!(tokenize_cmd("  a\n\tb\u{a0}c  "), ["a", "b", "c"]);
    assert_eq!(extract_recipe("x -ncmoe -1").moe_cpu_layers, None);
    assert_eq!(extract_recipe("x -ncmoe 007").moe_cpu_layers, Some(7.0));
    assert_eq!(extract_recipe("x -m").model_path, None);
}

// ---- GET /api/serving projection (`serving/llamaConfig.test.ts`) --------------------------

fn fixture_models() -> Vec<Value> {
    let path = crate::record::tests::fixtures("llama-config.yaml");
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
