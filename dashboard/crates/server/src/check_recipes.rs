//! `bun run check:recipes` (plan bench-dashboard-rust-port WP-5, Decision 7): a port of
//! `scripts/{check-serving-recipes,servingRecipeConformance,servingVisibilityConformance}.ts`.
//! Two orthogonal conformance policies over `llama/config.yaml`'s served entries, parsed once
//! by [`crate::serving::parse_served_aliases`] (the same reader `GET /api/serving` uses):
//!   - the capability-recipe policy: every served CHAT entry against
//!     `docs/model-capabilities.md`'s required key set;
//!   - the visibility/gateway policy: EVERY served key against `router/visibility.json` and the
//!     `router/gateway.config.json` max-tokens crosswalk.
//!
//! Findings are domain-authored text and byte-exact with the TS: same order (every recipe
//! finding, then every visibility finding, each in config order), same wording. The aliases are
//! the `ServingAlias` wire values, carried through as the YAML has them, so every check below
//! applies the TS's JS semantics (`typeof`, `===`, `String(x)`, `.includes`) to arbitrary
//! values — that is what lets the gate see a malformed entry at all. Where the TS would throw a
//! `TypeError` (a non-array `aliases`/`input_modalities` reaching `.join`/`.filter`), this
//! returns `Err` and the binary exits 1 with nothing on stdout, as Bun does.
use crate::jsrt;
use bd_domain::contract::{
    SERVING_CONSUMER_ORDER, SERVING_VERIFIED_DATE_PATTERN, SERVING_VISIBILITY_ENTRY,
    SERVING_VISIBILITY_ENVELOPE, SERVING_VISIBILITY_SCHEMA_V1,
};
use bd_domain::validate::{self, pattern_regex, Seg};
use serde_json::{Map, Value};
use std::collections::HashMap;

/// One non-conformance (`RecipeFinding`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finding {
    pub model_key: String,
    pub rule: String,
    pub detail: String,
}

/// `formatFinding`.
pub fn format_finding(f: &Finding) -> String {
    format!("{}: {} — {}", f.model_key, f.rule, f.detail)
}

/// `<repo>/llama/config.yaml`, resolved from this crate's own location (the TS resolved it
/// from `import.meta.dir`), never from the cwd.
pub fn default_config_path() -> String {
    jsrt::path_join(env!("CARGO_MANIFEST_DIR"), "../../../llama/config.yaml")
}

/// `path.dirname` (POSIX).
fn dirname(p: &str) -> String {
    if p.is_empty() {
        return ".".into();
    }
    let trimmed = p.trim_end_matches('/');
    if trimmed.is_empty() {
        return "/".into();
    }
    match trimmed.rfind('/') {
        None => ".".into(),
        Some(i) => {
            let d = trimmed[..i].trim_end_matches('/');
            if d.is_empty() {
                "/".into()
            } else {
                d.into()
            }
        }
    }
}

/// `resolveVisibilityPath`: relative to the CONFIG, so a fixture run resolves its own siblings.
pub fn resolve_visibility_path(config_path: &str) -> String {
    jsrt::path_join(&dirname(config_path), "../router/visibility.json")
}

/// `resolveGatewayPath`.
pub fn resolve_gateway_path(config_path: &str) -> String {
    jsrt::path_join(&dirname(config_path), "../router/gateway.config.json")
}

/// `parsePathArg`: `--<name> <path>` (first occurrence, needs a following arg), else
/// `--<name>=<path>`, else `fallback`.
pub fn parse_path_arg(argv: &[String], name: &str, fallback: &str) -> String {
    let flag = format!("--{name}");
    if let Some(i) = argv.iter().position(|a| *a == flag) {
        if let Some(v) = argv.get(i + 1) {
            return v.clone();
        }
    }
    let prefix = format!("--{name}=");
    if let Some(a) = argv.iter().find(|a| a.starts_with(&prefix)) {
        return a[prefix.len()..].to_string();
    }
    fallback.to_string()
}

// ---- JS value semantics -------------------------------------------------------------------

/// `String(v)` for a JSON value.
pub fn js_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "null".into(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => jsrt::num_str(n.as_f64().unwrap_or(f64::NAN)),
        Value::Array(items) => js_join(items, ","),
        Value::Object(_) => "[object Object]".into(),
    }
}

/// `Array.prototype.join(sep)`: `null`/`undefined` elements are empty.
fn js_join(items: &[Value], sep: &str) -> String {
    items
        .iter()
        .map(|v| {
            if v.is_null() {
                String::new()
            } else {
                js_str(v)
            }
        })
        .collect::<Vec<_>>()
        .join(sep)
}

/// `a === b` on JSON values (objects/arrays are distinct references, never equal).
fn strict_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x.as_f64() == y.as_f64(),
        (Value::String(x), Value::String(y)) => x == y,
        (Value::Bool(x), Value::Bool(y)) => x == y,
        (Value::Null, Value::Null) => true,
        _ => false,
    }
}

/// `ToNumber(v)` (for a relational comparison).
fn to_number(v: &Value) -> f64 {
    match v {
        Value::Number(n) => n.as_f64().unwrap_or(f64::NAN),
        Value::String(s) => bd_domain::js::number(s),
        Value::Bool(b) => f64::from(u8::from(*b)),
        Value::Null => 0.0,
        Value::Array(_) => bd_domain::js::number(&js_str(v)),
        Value::Object(_) => f64::NAN,
    }
}

/// `v.length > 0` (a value without `length` compares `undefined > 0`, false).
fn length_positive(v: &Value) -> bool {
    match v {
        Value::Array(a) => !a.is_empty(),
        Value::String(s) => !s.is_empty(),
        _ => false,
    }
}

/// `v.includes(needle)` with a string needle: array membership, or a substring for a string.
/// `None` when `v` has no `includes` (the TS throws a TypeError).
fn includes(v: &Value, needle: &str) -> Option<bool> {
    match v {
        Value::Array(a) => Some(a.iter().any(|x| x.as_str() == Some(needle))),
        Value::String(s) => Some(s.contains(needle)),
        _ => None,
    }
}

/// `Object.keys(v)` for a JSON value.
fn object_keys(v: &Value) -> Vec<String> {
    match v {
        Value::Object(m) => jsrt::js_entries(m)
            .into_iter()
            .map(|(k, _)| k.clone())
            .collect(),
        Value::Array(a) => (0..a.len()).map(|i| i.to_string()).collect(),
        Value::String(s) => (0..s.encode_utf16().count())
            .map(|i| i.to_string())
            .collect(),
        _ => Vec::new(),
    }
}

/// `v[key]` for a JSON value (`undefined` is `None`).
fn index<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    match v {
        Value::Object(m) => m.get(key),
        Value::Array(a) => key
            .parse::<usize>()
            .ok()
            .filter(|_| bd_domain::jsout::is_array_index(key))
            .and_then(|i| a.get(i)),
        _ => None,
    }
}

/// `v?.key ?? null`.
fn get_nn<'a>(v: &'a Value, key: &str) -> &'a Value {
    static NULL: Value = Value::Null;
    v.as_object().and_then(|m| m.get(key)).unwrap_or(&NULL)
}

fn type_error(what: &str) -> String {
    format!("TypeError: {what} is not a function")
}

// ---- the capability-recipe policy (servingRecipeConformance.ts) ---------------------------

/// The five approved source kinds, as the host each is served from.
const APPROVED_SOURCE_HOSTS: &[(&str, &str)] = &[
    ("huggingface.co", "lab or unsloth HF model page"),
    ("docs.vllm.ai", "vLLM recipe"),
    ("docs.sglang.ai", "SGLang recipe"),
    ("docs.unsloth.ai", "unsloth serving guide"),
    (
        "unsloth.ai",
        "unsloth serving guide (unsloth.ai/docs, where docs.unsloth.ai 301s since 2026-09)",
    ),
];

/// Claude Code's default request `max_tokens` (`CLAUDE_CODE_MAX_OUTPUT_TOKENS`).
pub const CLAUDE_CODE_DEFAULT_MAX_OUTPUT_TOKENS: f64 = 32000.0;

const SAMPLING_KEYS: &[&str] = &[
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "repetition_penalty",
];
const SAMPLING_FIELDS: &[&str] = &[
    "temperature",
    "topP",
    "topK",
    "minP",
    "presencePenalty",
    "repetitionPenalty",
];

fn advisory_keys() -> Vec<String> {
    let mut keys: Vec<String> = SAMPLING_KEYS
        .iter()
        .map(|k| format!("sampling.{k}"))
        .collect();
    keys.push("max_output.answer_tokens".into());
    keys
}

const REASONING_LEVELS: &[&str] = &["none", "budgeted", "request-controllable", "always-on"];
const INPUT_MODALITIES: &[&str] = &["text", "image"];
const OUTPUT_MODALITIES: &[&str] = &["text", "embedding", "score"];

/// `flagValue`: `None` when the flag is absent; `Some(None)` for a value-less flag.
fn flag_value<'a>(alias: &'a Value, flag: &str) -> Option<Option<&'a str>> {
    alias
        .get("flags")
        .and_then(|f| f.get("allFlags"))
        .and_then(Value::as_array)?
        .iter()
        .find(|e| e.get("flag").and_then(Value::as_str) == Some(flag))
        .map(|e| e.get("value").and_then(Value::as_str))
}

fn has_flag(alias: &Value, flag: &str) -> bool {
    flag_value(alias, flag).is_some()
}

/// `intFlag`: `/^-?\d+$/` then `parseInt`.
fn int_flag(alias: &Value, flag: &str) -> Option<f64> {
    let raw = flag_value(alias, flag)??;
    let digits = raw.strip_prefix('-').unwrap_or(raw);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    raw.parse::<f64>().ok()
}

fn num_or_null(n: Option<f64>) -> Value {
    n.map_or(Value::Null, bd_domain::js::json_number)
}

fn opt_str(n: Option<f64>) -> String {
    n.map_or_else(|| "null".into(), jsrt::num_str)
}

/// `String(flagValue(...))` for a present flag (`null` when value-less).
fn raw_str(raw: Option<&str>) -> String {
    raw.map_or_else(|| "null".into(), str::to_string)
}

fn is_chat_entry(alias: &Value) -> bool {
    if has_flag(alias, "--embeddings") || has_flag(alias, "--reranking") {
        return false;
    }
    let cap = get_nn(alias, "capability");
    if let Some(outputs) = cap.as_object().and_then(|c| c.get("outputModalities")) {
        if length_positive(outputs) && includes(outputs, "text") == Some(false) {
            return false;
        }
    }
    true
}

struct Sink<'a> {
    model_key: &'a str,
    findings: &'a mut Vec<Finding>,
}

impl Sink<'_> {
    fn push(&mut self, rule: &str, detail: String) {
        self.findings.push(Finding {
            model_key: self.model_key.to_string(),
            rule: rule.to_string(),
            detail,
        });
    }
}

fn check_sources(alias: &Value, out: &mut Sink<'_>) {
    let sources = get_nn(get_nn(alias, "capability"), "sources");
    if sources.is_null() {
        out.push("sources-missing", "no `metadata.sources` map; the seven advisory keys each need a citation or an explicit gap record".into());
        return;
    }
    let advisory = advisory_keys();
    let present = object_keys(sources);
    let missing: Vec<&String> = advisory.iter().filter(|k| !present.contains(k)).collect();
    let extra: Vec<&String> = present.iter().filter(|k| !advisory.contains(k)).collect();
    if !missing.is_empty() || !extra.is_empty() {
        let mut parts = Vec::new();
        if !missing.is_empty() {
            parts.push(format!(
                "missing {}",
                missing
                    .iter()
                    .map(|s| s.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
        if !extra.is_empty() {
            parts.push(format!(
                "unexpected {}",
                extra
                    .iter()
                    .map(|s| s.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
        out.push(
            "sources-keys",
            format!(
                "`sources` must carry exactly the seven advisory keys — {}",
                parts.join("; ")
            ),
        );
    }
    let hosts = APPROVED_SOURCE_HOSTS
        .iter()
        .map(|(_, d)| *d)
        .collect::<Vec<_>>()
        .join(", ");
    for key in &present {
        let entry = match sources {
            Value::String(_) => Some(&Value::Null), // a character: not a mapping
            other => index(other, key),
        };
        let entry = match entry {
            Some(e @ (Value::Object(_) | Value::Array(_))) => e,
            _ => {
                out.push(
                    "source-shape",
                    format!("`sources.\"{key}\"` is not a mapping"),
                );
                continue;
            }
        };
        let field = |name: &str| index(entry, name);
        let url = match field("url") {
            Some(Value::String(u)) if !u.is_empty() => u.as_str(),
            _ => {
                out.push("source-url", format!("`sources.\"{key}\".url` is missing"));
                continue;
            }
        };
        if url == "none" {
            let ok = matches!(field("reason"), Some(Value::String(r)) if !bd_domain::js::trim(r).is_empty());
            if !ok {
                out.push(
                    "source-reason",
                    format!("`sources.\"{key}\"` records url \"none\" without a `reason`"),
                );
            }
            continue;
        }
        let (host, path) = match url::Url::parse(url) {
            Ok(parsed) => {
                let host = (parsed.scheme() == "https").then(|| {
                    let h = parsed.host_str().unwrap_or("");
                    h.strip_prefix("www.").unwrap_or(h).to_string()
                });
                (host, parsed.path().to_string())
            }
            Err(_) => (None, String::new()),
        };
        let approved = host
            .as_deref()
            .is_some_and(|h| APPROVED_SOURCE_HOSTS.iter().any(|(k, _)| *k == h));
        if !approved || path.encode_utf16().count() <= 1 {
            out.push(
                "source-url",
                format!("`sources.\"{key}\".url` is not one of the approved source kinds ({hosts}) or the literal \"none\": {url}"),
            );
            continue;
        }
        let verified = field("verified");
        let ok = matches!(verified, Some(Value::String(v)) if pattern_regex(&SERVING_VERIFIED_DATE_PATTERN).is_match(v));
        if !ok {
            let shown = verified.map_or_else(|| "undefined".into(), jsrt::stringify);
            out.push(
                "source-verified",
                format!("`sources.\"{key}\".verified` must be a YYYY-MM-DD date, got {shown}"),
            );
        }
    }
}

fn check_sampling_declared(alias: &Value, out: &mut Sink<'_>) {
    let cap = get_nn(alias, "capability");
    let sampling = get_nn(cap, "sampling");
    let sources = get_nn(cap, "sources");
    for (key, field) in SAMPLING_KEYS.iter().zip(SAMPLING_FIELDS) {
        if get_nn(sampling, field).is_number() {
            continue;
        }
        let gap = sources
            .as_object()
            .and_then(|m| m.get(&format!("sampling.{key}")));
        if let Some(Value::Object(g)) = gap {
            if g.get("url").and_then(Value::as_str) == Some("none") {
                continue;
            }
        }
        out.push(
            "sampling-undeclared",
            format!("`sampling.{key}` has no value and no `sources.\"sampling.{key}\"` gap record"),
        );
    }
}

/// `cap.<field>.filter(m => !vocab.has(m))`; `Err` when the value has no `filter`.
fn off_vocabulary(v: &Value, vocab: &[&str], what: &str) -> Result<Vec<Value>, String> {
    match v {
        Value::Array(a) => Ok(a
            .iter()
            .filter(|m| !m.as_str().is_some_and(|s| vocab.contains(&s)))
            .cloned()
            .collect()),
        _ => Err(type_error(&format!("cap.{what}.filter"))),
    }
}

fn check_projections(alias: &Value, out: &mut Sink<'_>) -> Result<(), String> {
    let cap = get_nn(alias, "capability");
    if cap.is_null() {
        return Ok(());
    }
    let serving = get_nn(cap, "serving");
    let max_output = get_nn(cap, "maxOutput");
    let context_length = get_nn(cap, "contextLength");

    if !context_length.is_null() {
        match int_flag(alias, "-c") {
            None => out.push(
                "projection",
                "`context_length` is declared but the `cmd:` carries no `-c` to project it from"
                    .into(),
            ),
            Some(c) if !strict_eq(&bd_domain::js::json_number(c), context_length) => out.push(
                "projection",
                format!(
                    "`context_length` {} ≠ `-c {}`",
                    js_str(context_length),
                    jsrt::num_str(c)
                ),
            ),
            Some(_) => {}
        }
    }

    let budget_flag = int_flag(alias, "--reasoning-budget");
    let has_budget = has_flag(alias, "--reasoning-budget");
    let expected_budget = if has_budget {
        num_or_null(budget_flag)
    } else {
        bd_domain::js::json_number(-1.0)
    };
    let declared_max = get_nn(max_output, "reasoningBudget");
    if !declared_max.is_null() && !strict_eq(declared_max, &expected_budget) {
        out.push(
            "projection",
            format!(
                "`max_output.reasoning_budget` {} ≠ {} ({})",
                js_str(declared_max),
                js_str(&expected_budget),
                if has_budget {
                    "`--reasoning-budget` in `cmd:`"
                } else {
                    "the flag is absent, so the documented default"
                }
            ),
        );
    }
    let serving_budget = get_nn(serving, "reasoningBudget");
    if !serving_budget.is_null() && !strict_eq(serving_budget, &expected_budget) {
        out.push(
            "projection",
            format!(
                "`serving.reasoning_budget` {} ≠ {} ({})",
                js_str(serving_budget),
                js_str(&expected_budget),
                if has_budget {
                    "`--reasoning-budget` in `cmd:`"
                } else {
                    "the flag is absent"
                }
            ),
        );
    }
    if serving_budget.is_null() && has_budget {
        out.push(
            "projection",
            format!(
                "`cmd:` carries `--reasoning-budget {}` but `serving.reasoning_budget` is absent",
                opt_str(budget_flag)
            ),
        );
    }

    let answer = get_nn(max_output, "answerTokens");
    if !answer.is_null() {
        let int = answer
            .as_f64()
            .filter(|f| f.is_finite() && f.fract() == 0.0);
        match int {
            Some(a) if a >= 1.0 => {
                if a > CLAUDE_CODE_DEFAULT_MAX_OUTPUT_TOKENS {
                    out.push(
                        "answer-cap",
                        format!(
                            "`max_output.answer_tokens` {} > {} (Claude Code's default max_tokens ceiling)",
                            js_str(answer),
                            jsrt::num_str(CLAUDE_CODE_DEFAULT_MAX_OUTPUT_TOKENS)
                        ),
                    );
                } else if !context_length.is_null() && a > to_number(context_length) {
                    out.push(
                        "answer-cap",
                        format!(
                            "`max_output.answer_tokens` {} > `context_length` {}",
                            js_str(answer),
                            js_str(context_length)
                        ),
                    );
                }
            }
            _ => out.push(
                "answer-cap",
                format!(
                    "`max_output.answer_tokens` {} is not a positive integer (a gap is recorded in `sources`, never as -1 or 0 here)",
                    js_str(answer)
                ),
            ),
        }
    }

    for (key, flag, field) in [
        ("serving.ubatch", "-ub", "ubatch"),
        (
            "serving.moe_cpu_offload_layers",
            "-ncmoe",
            "moeCpuOffloadLayers",
        ),
        ("serving.reasoning_mode", "--reasoning", "reasoningMode"),
    ] {
        let declared = get_nn(serving, field);
        let raw = flag_value(alias, flag);
        match (declared.is_null(), raw) {
            (true, None) => {}
            (true, Some(r)) => out.push(
                "projection",
                format!(
                    "`cmd:` carries `{flag} {}` but `{key}` is absent",
                    raw_str(r)
                ),
            ),
            (false, None) => out.push(
                "projection",
                format!(
                    "`{key}` is declared ({}) but the `cmd:` carries no `{flag}`",
                    js_str(declared)
                ),
            ),
            (false, Some(r)) => {
                let expected = if declared.is_number() {
                    opt_str(int_flag(alias, flag))
                } else {
                    raw_str(r)
                };
                if expected != js_str(declared) {
                    out.push(
                        "projection",
                        format!("`{key}` {} ≠ `{flag} {}`", js_str(declared), raw_str(r)),
                    );
                }
            }
        }
    }

    let kv = get_nn(serving, "kvCacheType");
    if !kv.is_null() {
        let ctk = flag_value(alias, "-ctk").flatten();
        let expected_kv = match ctk {
            Some(s) => s.to_string(),
            None => match flag_value(alias, "-ctv") {
                Some(v) => raw_str(v),
                None => "f16".into(),
            },
        };
        if kv.as_str() != Some(expected_kv.as_str()) {
            out.push(
                "projection",
                format!(
                    "`serving.kv_cache_type` \"{}\" ≠ \"{expected_kv}\" ({})",
                    js_str(kv),
                    if ctk.is_some() {
                        "`-ctk` in `cmd:`"
                    } else {
                        "no `-ctk`/`-ctv`, so the llama.cpp default"
                    }
                ),
            );
        }
    }

    let fa_declared = get_nn(serving, "flashAttention");
    if !fa_declared.is_null() {
        let fa = flag_value(alias, "-fa").flatten() == Some("on");
        if !strict_eq(fa_declared, &Value::Bool(fa)) {
            out.push(
                "projection",
                format!(
                    "`serving.flash_attention` {} ≠ {fa} (`-fa on` {} in `cmd:`)",
                    js_str(fa_declared),
                    if fa { "is" } else { "is not" }
                ),
            );
        }
    }

    let quant = get_nn(serving, "quant");
    if !quant.is_null() {
        let model_path = get_nn(get_nn(alias, "recipe"), "modelPath");
        match model_path.as_str() {
            None => out.push(
                "projection",
                format!(
                    "`serving.quant` \"{}\" cannot be checked — the `cmd:` carries no `-m`",
                    js_str(quant)
                ),
            ),
            Some(p) => {
                let file = p.rsplit('/').next().unwrap_or("");
                let q = js_str(quant);
                if !file.contains(q.as_str()) {
                    out.push(
                        "projection",
                        format!(
                            "`serving.quant` \"{q}\" does not appear in the served GGUF filename {file}"
                        ),
                    );
                }
            }
        }
    }

    let inputs = get_nn(cap, "inputModalities");
    if length_positive(inputs) {
        let bad = off_vocabulary(inputs, INPUT_MODALITIES, "inputModalities")?;
        if !bad.is_empty() {
            out.push(
                "vocabulary",
                format!(
                    "`input_modalities` carries {} — vocabulary is text, image",
                    js_join(&bad, ", ")
                ),
            );
        }
        let claims_image = includes(inputs, "image") == Some(true);
        let mmproj = has_flag(alias, "--mmproj");
        if claims_image != mmproj {
            out.push(
                "projection",
                format!(
                    "`input_modalities` {} \"image\" but the `cmd:` {} `--mmproj`",
                    if claims_image { "claims" } else { "omits" },
                    if mmproj { "carries" } else { "carries no" }
                ),
            );
        }
    }
    let outputs = get_nn(cap, "outputModalities");
    if length_positive(outputs) {
        let bad = off_vocabulary(outputs, OUTPUT_MODALITIES, "outputModalities")?;
        if !bad.is_empty() {
            out.push(
                "vocabulary",
                format!(
                    "`output_modalities` carries {} — vocabulary is text, embedding, score",
                    js_join(&bad, ", ")
                ),
            );
        }
    }
    let reasoning = get_nn(cap, "reasoning");
    if !reasoning.is_null()
        && !reasoning
            .as_str()
            .is_some_and(|r| REASONING_LEVELS.contains(&r))
    {
        out.push(
            "vocabulary",
            format!(
                "`reasoning` \"{}\" is not one of {}",
                js_str(reasoning),
                REASONING_LEVELS.join(", ")
            ),
        );
    }
    Ok(())
}

fn check_required_keys(alias: &Value, out: &mut Sink<'_>) {
    let cap = get_nn(alias, "capability");
    if cap.is_null() {
        out.push("missing-key", "no `metadata:` block at all".into());
        return;
    }
    let serving = get_nn(cap, "serving");
    let max_output = get_nn(cap, "maxOutput");
    let set = |v: &Value| !v.is_null();
    let required: [(&str, bool); 11] = [
        ("context_length", set(get_nn(cap, "contextLength"))),
        (
            "input_modalities",
            length_positive(get_nn(cap, "inputModalities")),
        ),
        (
            "output_modalities",
            length_positive(get_nn(cap, "outputModalities")),
        ),
        ("tool_calling", set(get_nn(cap, "toolCalling"))),
        ("reasoning", set(get_nn(cap, "reasoning"))),
        ("serving.quant", set(get_nn(serving, "quant"))),
        ("serving.kv_cache_type", set(get_nn(serving, "kvCacheType"))),
        (
            "serving.flash_attention",
            set(get_nn(serving, "flashAttention")),
        ),
        ("serving.mtp", set(get_nn(serving, "mtp"))),
        (
            "max_output.reasoning_budget",
            set(get_nn(max_output, "reasoningBudget")),
        ),
        (
            "max_output.answer_tokens",
            set(get_nn(max_output, "answerTokens")),
        ),
    ];
    for (key, ok) in required {
        if !ok {
            out.push(
                "missing-key",
                format!("`{key}` is required on every served chat entry"),
            );
        }
    }
}

/// `checkRecipeConformance`: chat entries only, in list order.
pub fn check_recipe_conformance(aliases: &[Value]) -> Result<Vec<Finding>, String> {
    let mut findings = Vec::new();
    for alias in aliases {
        if !is_chat_entry(alias) {
            continue;
        }
        let model_key = alias.get("modelKey").and_then(Value::as_str).unwrap_or("");
        let mut out = Sink {
            model_key,
            findings: &mut findings,
        };
        check_required_keys(alias, &mut out);
        check_sampling_declared(alias, &mut out);
        check_sources(alias, &mut out);
        check_projections(alias, &mut out)?;
    }
    Ok(findings)
}

// ---- the visibility/gateway policy (servingVisibilityConformance.ts) -----------------------

const CLAMP_PROVIDER: &str = "claude-local";

fn issue_path(path: &[Seg]) -> String {
    path.iter()
        .map(|s| match s {
            Seg::Key(k) => k.clone(),
            Seg::Index(i) => i.to_string(),
        })
        .collect::<Vec<_>>()
        .join(".")
}

/// `parseVisibility`: the `models` map, or the problem naming why the file is unusable.
fn parse_visibility(text: Option<&str>) -> Result<Map<String, Value>, String> {
    let Some(text) = text else {
        return Err("the visibility declaration is missing or unreadable".into());
    };
    let parsed = jsrt::json_parse_js(text)
        .map_err(|m| format!("the visibility declaration is not valid JSON: {m}"))?;
    match validate::parse(&SERVING_VISIBILITY_ENVELOPE.node, &parsed) {
        Ok(v) => Ok(v
            .get("models")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default()),
        Err(issues) => {
            let (path, detail) = match issues.first() {
                None => (String::new(), "does not match".to_string()),
                Some(i) if i.path.is_empty() => (String::new(), i.message.clone()),
                Some(i) => (format!(" at `{}`", issue_path(&i.path)), i.message.clone()),
            };
            Err(format!(
                "the visibility declaration does not match the `{SERVING_VISIBILITY_SCHEMA_V1}` schema{path}: {detail}"
            ))
        }
    }
}

/// `parseGatewayCaps`: `claude-local.maxOutputTokens`, or `None` (fail-closed).
fn parse_gateway_caps(text: Option<&str>) -> Option<Map<String, Value>> {
    let parsed = jsrt::json_parse(text?).ok()?;
    let providers = parsed.get("providers")?.as_array()?;
    let provider = providers
        .iter()
        .find(|p| p.get("name").and_then(Value::as_str) == Some(CLAMP_PROVIDER))?;
    provider.get("maxOutputTokens")?.as_object().cloned()
}

fn consumers_order_problem(consumers: &[Value]) -> Option<String> {
    let canonical: Vec<Value> = SERVING_CONSUMER_ORDER
        .iter()
        .filter(|c| consumers.iter().any(|x| x.as_str() == Some(c)))
        .map(|c| Value::String((*c).into()))
        .collect();
    if consumers.len() != canonical.len() || canonical.iter().zip(consumers).any(|(c, x)| c != x) {
        return Some(format!(
            "`consumers` {} is duplicated or out of canonical order — expected {}",
            jsrt::stringify(&Value::Array(consumers.to_vec())),
            jsrt::stringify(&Value::Array(canonical))
        ));
    }
    None
}

fn check_visibility_entry(
    alias: &Value,
    models: &Map<String, Value>,
    caps: Option<&Map<String, Value>>,
    claimed: &mut HashMap<String, String>,
    out: &mut Sink<'_>,
) -> Result<(), String> {
    let model_key = out.model_key.to_string();
    let Some(raw) = models.get(&model_key) else {
        out.push(
            "visibility-missing",
            "no entry in `router/visibility.json` — every served key must be declared there".into(),
        );
        return Ok(());
    };

    let entry = match validate::parse(&SERVING_VISIBILITY_ENTRY.node, raw) {
        Ok(v) => v,
        Err(issues) => {
            let issue = issues.first();
            let field = issue.and_then(|i| i.path.first());
            if field == Some(&Seg::Key("consumers".into())) {
                let detail = match raw.get("consumers") {
                    Some(Value::Array(cs)) => format!(
                        "`consumers` carries {} — the closed vocabulary is {}",
                        cs.iter()
                            .filter(|c| !c
                                .as_str()
                                .is_some_and(|s| SERVING_CONSUMER_ORDER.contains(&s)))
                            .map(jsrt::stringify)
                            .collect::<Vec<_>>()
                            .join(", "),
                        SERVING_CONSUMER_ORDER.join(", ")
                    ),
                    _ => "`consumers` is not an array".into(),
                };
                out.push("visibility-vocabulary", detail);
                return Ok(());
            }
            let detail = issue.map_or_else(
                || "does not match the declared entry shape".to_string(),
                |i| i.message.clone(),
            );
            let text = if field == Some(&Seg::Key("gateway_id".into())) {
                format!(
                    "`gateway_id` {} is neither null nor a string",
                    jsrt::stringify(get_nn(raw, "gateway_id"))
                )
            } else {
                format!(
                    "entry does not match the `{SERVING_VISIBILITY_SCHEMA_V1}` entry shape: {detail}"
                )
            };
            out.push("visibility-gateway-id", text);
            return Ok(());
        }
    };

    let consumers = entry
        .get("consumers")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if let Some(problem) = consumers_order_problem(&consumers) {
        out.push("visibility-vocabulary", problem);
        return Ok(());
    }

    let gateway_id = entry.get("gateway_id").and_then(Value::as_str);
    let aliases = get_nn(alias, "aliases");
    if let Some(gid) = gateway_id {
        let member = includes(aliases, gid).ok_or_else(|| type_error("alias.aliases.includes"))?;
        if !member {
            let joined = match aliases {
                Value::Array(a) => js_join(a, ", "),
                _ => return Err(type_error("alias.aliases.join")),
            };
            let shown = if joined.is_empty() {
                "none".into()
            } else {
                joined
            };
            out.push(
                "visibility-gateway-id",
                format!("`gateway_id` \"{gid}\" is not one of this key's aliases ({shown})"),
            );
            return Ok(());
        }
    }
    let Some(gid) = gateway_id else {
        if consumers.iter().any(|c| c.as_str() == Some("cc")) {
            out.push(
                "visibility-cc-requires-gateway-id",
                "`consumers` includes `cc` but `gateway_id` is null — the Claude Code picker can only emit a gateway id".into(),
            );
        }
        return Ok(());
    };
    if let Some(owner) = claimed.get(gid) {
        out.push(
            "visibility-duplicate-gateway-id",
            format!("`gateway_id` \"{gid}\" is already claimed by {owner} — one picker row cannot answer to two declarations"),
        );
        return Ok(());
    }
    claimed.insert(gid.to_string(), model_key);

    let declared = get_nn(
        get_nn(get_nn(alias, "capability"), "maxOutput"),
        "answerTokens",
    );
    match caps.and_then(|c| c.get(gid)) {
        None => out.push(
            "max-tokens-crosswalk",
            format!("`{CLAMP_PROVIDER}.maxOutputTokens[\"{gid}\"]` is missing from `router/gateway.config.json` — patch 0010's clamp reads that map"),
        ),
        Some(cap) if declared.is_null() => out.push(
            "max-tokens-crosswalk",
            format!("`{CLAMP_PROVIDER}.maxOutputTokens[\"{gid}\"]` is {} but this key declares no `max_output.answer_tokens` to crosswalk it against", js_str(cap)),
        ),
        Some(cap) if !strict_eq(cap, declared) => out.push(
            "max-tokens-crosswalk",
            format!("`{CLAMP_PROVIDER}.maxOutputTokens[\"{gid}\"]` {} ≠ `max_output.answer_tokens` {}", js_str(cap), js_str(declared)),
        ),
        Some(_) => {}
    }
    Ok(())
}

/// `checkVisibilityConformance`: every served key, in list order; an unusable declaration is
/// one finding for the whole file.
pub fn check_visibility_conformance(
    aliases: &[Value],
    visibility_text: Option<&str>,
    gateway_text: Option<&str>,
) -> Result<Vec<Finding>, String> {
    let mut findings = Vec::new();
    let models = match parse_visibility(visibility_text) {
        Ok(m) => m,
        Err(problem) => {
            findings.push(Finding {
                model_key: "router/visibility.json".into(),
                rule: "visibility-unreadable".into(),
                detail: problem,
            });
            return Ok(findings);
        }
    };
    let caps = parse_gateway_caps(gateway_text);
    let mut claimed = HashMap::new();
    for alias in aliases {
        let model_key = alias.get("modelKey").and_then(Value::as_str).unwrap_or("");
        let mut out = Sink {
            model_key,
            findings: &mut findings,
        };
        check_visibility_entry(alias, &models, caps.as_ref(), &mut claimed, &mut out)?;
    }
    Ok(findings)
}

/// `checkServingRecipes`: every recipe finding, then every visibility finding. `Err` is the
/// TS's uncaught throw (YAML error, a TypeError on a malformed value).
pub fn check_serving_recipes(
    config_text: &str,
    visibility_text: Option<&str>,
    gateway_text: Option<&str>,
) -> Result<Vec<Finding>, String> {
    let aliases = crate::serving::parse_served_aliases(config_text)?;
    let mut findings = check_recipe_conformance(&aliases)?;
    findings.extend(check_visibility_conformance(
        &aliases,
        visibility_text,
        gateway_text,
    )?);
    Ok(findings)
}

/// `Bun.file(path).text()`.
fn read_text(path: &str) -> Result<String, String> {
    let bytes = std::fs::read(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            format!("ENOENT: no such file or directory, open '{path}'")
        } else {
            format!("{e}, open '{path}'")
        }
    })?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

/// `checkServingRecipesFile`: an unreadable companion is `null` (a finding), an unreadable
/// config is an error.
pub fn check_serving_recipes_file(
    path: &str,
    visibility_path: &str,
    gateway_path: &str,
) -> Result<Vec<Finding>, String> {
    let config = read_text(path)?;
    let visibility = read_text(visibility_path).ok();
    let gateway = read_text(gateway_path).ok();
    check_serving_recipes(&config, visibility.as_deref(), gateway.as_deref())
}

/// The CLI: stdout lines and exit code, exactly as `check-serving-recipes.ts` prints them.
pub fn run(argv: &[String]) -> Result<(Vec<String>, i32), String> {
    let path = parse_path_arg(argv, "config", &default_config_path());
    let findings = check_serving_recipes_file(
        &path,
        &parse_path_arg(argv, "visibility", &resolve_visibility_path(&path)),
        &parse_path_arg(argv, "gateway", &resolve_gateway_path(&path)),
    )?;
    let mut lines: Vec<String> = findings.iter().map(format_finding).collect();
    if !findings.is_empty() {
        let n = findings.len();
        lines.push(format!(
            "{n} non-conforming entr{} in {path}",
            if n == 1 { "y" } else { "ies" }
        ));
        return Ok((lines, 1));
    }
    lines.push(format!("ok — every served chat entry in {path} conforms to docs/model-capabilities.md, and every served key is declared in router/visibility.json"));
    Ok((lines, 0))
}

#[cfg(test)]
#[path = "check_recipes_tests.rs"]
mod tests;
