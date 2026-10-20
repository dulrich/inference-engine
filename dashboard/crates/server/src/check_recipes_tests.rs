//! `apps/api/src/serving/checkRecipes.test.ts`, carried over case for case, plus unit cases for
//! the JS-semantics helpers the port leans on.
use super::*;

fn fixture(name: &str) -> String {
    format!(
        "{}/../../fixtures/serving/{name}",
        env!("CARGO_MANIFEST_DIR")
    )
}

fn read(name: &str) -> String {
    std::fs::read_to_string(fixture(name)).expect("fixture")
}

fn file_findings(prefix: &str) -> Vec<Finding> {
    check_serving_recipes_file(
        &fixture(&format!("{prefix}.yaml")),
        &fixture(&format!("{prefix}-catalog.json")),
    )
    .expect("fixture parses")
}

fn keys_rules(findings: &[Finding]) -> Vec<(String, String)> {
    findings
        .iter()
        .map(|f| (f.model_key.clone(), f.rule.clone()))
        .collect()
}

fn pairs(list: &[(&str, &str)]) -> Vec<(String, String)> {
    list.iter()
        .map(|(a, b)| ((*a).to_string(), (*b).to_string()))
        .collect()
}

fn is_visibility(f: &Finding) -> bool {
    f.rule.starts_with("visibility-") || f.rule == "max-tokens-crosswalk"
}

#[test]
fn a_conforming_config_yields_no_findings() {
    assert!(file_findings("conforming").is_empty());
}

#[test]
fn each_non_conforming_entry_yields_exactly_one_finding_of_the_expected_rule() {
    let findings = file_findings("non-conforming");
    assert_eq!(
        keys_rules(&findings),
        pairs(&[
            ("missing-mtp-chat", "missing-key"),
            ("unapproved-source-chat", "source-url"),
            ("gap-no-reason-chat", "source-reason"),
            ("bad-verified-chat", "source-verified"),
            ("context-mismatch-chat", "projection"),
            ("kv-claim-chat", "projection"),
            ("budget-claim-chat", "projection"),
            ("answer-cap-chat", "answer-cap"),
            ("bad-reasoning-chat", "vocabulary"),
            ("undeclared-chat", "visibility-missing"),
            ("bad-consumers-chat", "visibility-vocabulary"),
            ("alien-gateway-id-chat", "visibility-gateway-id"),
            ("cc-null-gateway-chat", "visibility-cc-requires-gateway-id"),
            ("dup-second-chat", "visibility-duplicate-gateway-id"),
            ("crosswalk-chat", "max-tokens-crosswalk"),
        ])
    );
    assert_eq!(
        format_finding(&findings[4]),
        "context-mismatch-chat: projection — `context_length` 65536 ≠ `-c 131072`"
    );
}

#[test]
fn embed_rerank_entries_are_out_of_scope_and_never_reported() {
    let findings = file_findings("non-conforming");
    assert!(!findings.iter().any(|f| f.model_key == "fixture-embed-f16"));
}

#[test]
fn the_default_config_path_resolves_to_the_repos_own_llama_config_not_the_cwd() {
    let default = default_config_path();
    assert!(default.ends_with("/llama/config.yaml"));
    assert!(!default.contains(".."));
    assert_eq!(parse_path_arg(&[], "config", &default), default);
    let args = |a: &[&str]| a.iter().map(|s| (*s).to_string()).collect::<Vec<_>>();
    assert_eq!(
        parse_path_arg(&args(&["--config", "/tmp/a.yaml"]), "config", &default),
        "/tmp/a.yaml"
    );
    assert_eq!(
        parse_path_arg(&args(&["--config=/tmp/b.yaml"]), "config", &default),
        "/tmp/b.yaml"
    );
    // `--config` as the last arg has no value: falls through to the inline form, then default.
    assert_eq!(
        parse_path_arg(&args(&["--config"]), "config", "fallback"),
        "fallback"
    );
}

#[test]
fn the_catalog_path_resolves_relative_to_the_config_and_is_overridable() {
    let default = default_config_path();
    assert!(resolve_catalog_path(&default).ends_with("/router/catalog/catalog.json"));
    assert_eq!(
        resolve_catalog_path("/r/llama/config.yaml"),
        "/r/router/catalog/catalog.json"
    );
    assert_eq!(
        resolve_catalog_path("config.yaml"),
        "../router/catalog/catalog.json"
    );
    let args = |a: &[&str]| a.iter().map(|s| (*s).to_string()).collect::<Vec<_>>();
    assert_eq!(
        parse_path_arg(&args(&["--catalog", "/tmp/c.json"]), "catalog", "fallback"),
        "/tmp/c.json"
    );
    assert_eq!(
        parse_path_arg(&args(&["--catalog=/tmp/d.json"]), "catalog", "fallback"),
        "/tmp/d.json"
    );
    assert_eq!(parse_path_arg(&[], "catalog", "fallback"), "fallback");
}

#[test]
fn a_missing_malformed_or_wrong_schema_declaration_is_one_finding_not_silence() {
    let config = read("conforming.yaml");
    for declaration in [
        None,
        Some("{not json"),
        Some(r#"{"schema":"model-catalog-v2","providers":{}}"#),
        Some(r#"{"schema":"model-catalog-v1"}"#),
        Some(r#"{"schema":"model-catalog-v1","providers":{"claude-local":{}}}"#),
        Some(r#"{"schema":"model-catalog-v1","providers":{},"exposures":[]}"#),
        Some("[]"),
    ] {
        let findings = check_serving_recipes(&config, declaration).unwrap();
        assert_eq!(
            findings.iter().map(|f| f.rule.as_str()).collect::<Vec<_>>(),
            ["visibility-unreadable"]
        );
        assert_eq!(findings[0].model_key, "router/catalog/catalog.json");
    }
}

#[test]
fn an_off_vocabulary_consumers_token_reports_on_its_own_key_and_does_not_blind_the_rest() {
    let config = read("non-conforming.yaml");
    let mut declaration: Value =
        serde_json::from_str(&read("non-conforming-catalog.json")).unwrap();
    declaration["exposures"]["bad-consumers-chat"]["consumers"] = serde_json::json!(["crucible"]);
    let text = declaration.to_string();
    let findings = check_serving_recipes(&config, Some(&text)).unwrap();
    assert!(!findings.iter().any(|f| f.rule == "visibility-unreadable"));
    let vis: Vec<Finding> = findings
        .iter()
        .filter(|f| is_visibility(f))
        .cloned()
        .collect();
    assert_eq!(
        keys_rules(&vis),
        pairs(&[
            ("undeclared-chat", "visibility-missing"),
            ("bad-consumers-chat", "visibility-vocabulary"),
            ("alien-gateway-id-chat", "visibility-gateway-id"),
            ("cc-null-gateway-chat", "visibility-cc-requires-gateway-id"),
            ("dup-second-chat", "visibility-duplicate-gateway-id"),
            ("crosswalk-chat", "max-tokens-crosswalk"),
        ])
    );
    let bad = findings
        .iter()
        .find(|f| f.model_key == "bad-consumers-chat")
        .unwrap();
    assert_eq!(
        format_finding(bad),
        "bad-consumers-chat: visibility-vocabulary — `consumers` carries \"crucible\" — the closed vocabulary is cc, openwebui"
    );
}

#[test]
fn a_wrong_typed_gateway_id_reports_on_its_own_key() {
    let config = read("conforming.yaml");
    let mut declaration: Value = serde_json::from_str(&read("conforming-catalog.json")).unwrap();
    let entry = &mut declaration["providers"]["claude-local"]["models"][0];
    let key = entry["modelKey"].as_str().unwrap().to_string();
    entry["id"] = serde_json::json!(42);
    let text = declaration.to_string();
    let findings = check_serving_recipes(&config, Some(&text)).unwrap();
    assert!(!findings.iter().any(|f| f.rule == "visibility-unreadable"));
    let own = findings
        .iter()
        .find(|f| f.model_key == key && f.rule == "visibility-gateway-id")
        .expect("own finding");
    assert_eq!(own.detail, "`gateway_id` 42 is neither null nor a string");
}

#[test]
fn findings_are_grouped_by_checker_not_interleaved_per_alias() {
    let config = read("non-conforming.yaml");
    let mut declaration: Value =
        serde_json::from_str(&read("non-conforming-catalog.json")).unwrap();
    declaration["exposures"]
        .as_object_mut()
        .unwrap()
        .shift_remove("missing-mtp-chat");
    let text = declaration.to_string();
    let findings = check_serving_recipes(&config, Some(&text)).unwrap();
    let own: Vec<(usize, &str)> = findings
        .iter()
        .enumerate()
        .filter(|(_, f)| f.model_key == "missing-mtp-chat")
        .map(|(i, f)| (i, f.rule.as_str()))
        .collect();
    assert_eq!(
        own.iter().map(|r| r.1).collect::<Vec<_>>(),
        ["missing-key", "visibility-missing"]
    );
    assert_eq!(own[0].0, 0);
    assert!(own[1].0 > 1);
    let last_recipe = findings.iter().rposition(|f| !is_visibility(f)).unwrap();
    let first_visibility = findings.iter().position(is_visibility).unwrap();
    assert!(last_recipe < first_visibility);
}

#[test]
fn a_catalog_with_no_max_output_tokens_fails_the_crosswalk_for_every_declared_id() {
    let config = read("conforming.yaml");
    let mut catalog: Value = serde_json::from_str(&read("conforming-catalog.json")).unwrap();
    for e in catalog["providers"]["claude-local"]["models"]
        .as_array_mut()
        .unwrap()
    {
        e.as_object_mut().unwrap().remove("maxOutputTokens");
    }
    let text = catalog.to_string();
    let findings = check_serving_recipes(&config, Some(&text)).unwrap();
    assert_eq!(
        keys_rules(&findings),
        pairs(&[
            ("fixture-chat-UD-Q4_K_XL", "max-tokens-crosswalk"),
            ("fixture-moe-UD-IQ3_S", "max-tokens-crosswalk"),
        ])
    );
    assert!(format_finding(&findings[0]).contains("is missing from"));
}

#[test]
fn run_prints_the_ok_line_or_the_count_line_with_the_exit_code() {
    let args = |prefix: &str| {
        vec![
            format!("--config={}", fixture(&format!("{prefix}.yaml"))),
            "--catalog".to_string(),
            fixture(&format!("{prefix}-catalog.json")),
        ]
    };
    let (lines, code) = run(&args("conforming")).unwrap();
    assert_eq!(code, 0);
    assert_eq!(lines.len(), 1);
    assert!(lines[0].starts_with("ok — every served chat entry in "));
    let (lines, code) = run(&args("non-conforming")).unwrap();
    assert_eq!(code, 1);
    assert!(lines
        .last()
        .unwrap()
        .starts_with("15 non-conforming entries in "));
}

#[test]
fn every_served_key_is_checked_against_model_keys_and_exposures_alike() {
    let config = read("conforming.yaml");
    let mut catalog: Value = serde_json::from_str(&read("conforming-catalog.json")).unwrap();
    // Dropping the modelKey of a routed entry and an exposure record each surface as missing.
    catalog["providers"]["claude-local"]["models"][1]
        .as_object_mut()
        .unwrap()
        .remove("modelKey");
    catalog["exposures"]
        .as_object_mut()
        .unwrap()
        .shift_remove("fixture-vision-UD-Q4_K_XL");
    let findings = check_serving_recipes(&config, Some(&catalog.to_string())).unwrap();
    assert_eq!(
        keys_rules(&findings),
        pairs(&[
            ("fixture-vision-UD-Q4_K_XL", "visibility-missing"),
            ("fixture-moe-UD-IQ3_S", "visibility-missing"),
        ])
    );
}

#[test]
fn a_missing_config_is_an_error_not_a_finding() {
    let err = run(&["--config".into(), "/nonexistent/llama/config.yaml".into()]).unwrap_err();
    assert!(err.starts_with("ENOENT"));
}

#[test]
fn js_semantics_helpers() {
    use serde_json::json;
    assert_eq!(js_str(&json!([1, null, "a", [2, 3]])), "1,,a,2,3");
    assert_eq!(js_str(&json!({"a": 1})), "[object Object]");
    assert_eq!(js_str(&json!(1.5)), "1.5");
    assert!(strict_eq(&json!(1), &json!(1.0)));
    assert!(!strict_eq(&json!("1"), &json!(1)));
    assert!(!strict_eq(&json!({}), &json!({})));
    assert_eq!(includes(&json!("embedding,text"), "text"), Some(true));
    assert_eq!(includes(&json!(["textual"]), "text"), Some(false));
    assert_eq!(includes(&json!(3), "text"), None);
    assert_eq!(object_keys(&json!("ab")), ["0", "1"]);
    assert_eq!(
        object_keys(&json!({"b": 1, "2": 0, "a": 1})),
        ["2", "b", "a"]
    );
    assert_eq!(dirname("/a/b/c.yaml"), "/a/b");
    assert_eq!(dirname("c.yaml"), ".");
    assert_eq!(dirname("/c.yaml"), "/");
}
