//! Batch probe of the Rust validator (the Rust side of `bun run test:contract-agreement`,
//! `crates/domain/agreement/`; kept as a debugging probe once zod retires in WP-6): reads
//! `[[schemaName, input], …]` as JSON on stdin (`{"$undefined": true}` stands for an absent
//! input) and prints `[[success, data, issues], …]` from `validate`. An unknown schema name prints
//! `[null, name, null]`.
use bd_domain::contract::EXPORTS;
use bd_domain::validate::{issues_json, parse_opt};
use serde_json::{json, Value};
use std::io::Read;

fn main() {
    let mut raw = String::new();
    if std::io::stdin().read_to_string(&mut raw).is_err() {
        std::process::exit(2);
    }
    let Ok(cases) = serde_json::from_str::<Vec<(String, Value)>>(&raw) else {
        eprintln!("contract-check: stdin is not a JSON array of [schemaName, input] pairs");
        std::process::exit(2);
    };
    let out: Vec<Value> = cases
        .iter()
        .map(|(name, input)| {
            let Some(export) = EXPORTS.iter().find(|e| e.schema_name() == *name) else {
                return json!([null, name, null]);
            };
            let input = (input != &json!({"$undefined": true})).then_some(input);
            match parse_opt(&export.node, input) {
                Ok(v) => json!([true, v, null]),
                Err(issues) => json!([false, null, issues_json(&issues)]),
            }
        })
        .collect();
    print!("{}", bd_domain::jsout::stringify(&Value::Array(out)));
}
