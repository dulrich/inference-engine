//! The `GET /api/serving` reader: `llama/config.yaml` parsed into the served-alias projection
//! (model key, aliases, `name`, `ttl`, `checkEndpoint`, `flags`, `capability` and the
//! `extractRecipe` recipe over the macro-expanded `cmd`); `check_recipes` reuses
//! `parse_served_aliases`. Read per request, never cached, never written; YAML through
//! `yaml-rust2` with map order preserved.
use bd_domain::js::is_js_space;
use serde::Serialize;
use serde_json::{Map, Value};
use yaml_rust2::{Yaml, YamlLoader};

/// `ServingRecipe` (`extractRecipe`'s output).
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServingRecipe {
    pub model_path: Option<String>,
    pub draft_path: Option<String>,
    pub spec_type: Option<String>,
    pub moe_cpu_layers: Option<f64>,
    pub kv_k: String,
    pub kv_v: String,
}

/// `Bun.YAML.parse` -> JSON value (order-preserving map, integral reals as JS numbers).
pub fn yaml_parse(text: &str) -> Result<Value, String> {
    let docs = YamlLoader::load_from_str(text).map_err(|e| e.to_string())?;
    match docs.first() {
        None => Ok(Value::Null),
        Some(d) => Ok(yaml_to_json(d)),
    }
}

fn yaml_key(k: &Yaml) -> String {
    match k {
        Yaml::String(s) | Yaml::Real(s) => s.clone(),
        Yaml::Integer(i) => i.to_string(),
        Yaml::Boolean(b) => b.to_string(),
        Yaml::Null => String::new(),
        other => serde_json::to_string(&yaml_to_json(other)).unwrap_or_default(),
    }
}

fn yaml_to_json(y: &Yaml) -> Value {
    match y {
        Yaml::Real(s) => {
            let f = match s.trim_start_matches(['+', '-']) {
                ".inf" | ".Inf" | ".INF" => f64::INFINITY,
                ".nan" | ".NaN" | ".NAN" => f64::NAN,
                _ => s.parse::<f64>().unwrap_or(f64::NAN),
            };
            bd_domain::js::json_number(f)
        }
        Yaml::Integer(i) => bd_domain::js::json_number(*i as f64),
        Yaml::String(s) => Value::String(s.clone()),
        Yaml::Boolean(b) => Value::Bool(*b),
        Yaml::Array(items) => Value::Array(items.iter().map(yaml_to_json).collect()),
        Yaml::Hash(h) => Value::Object(
            h.iter()
                .map(|(k, v)| (yaml_key(k), yaml_to_json(v)))
                .collect::<Map<_, _>>(),
        ),
        Yaml::Null | Yaml::Alias(_) | Yaml::BadValue => Value::Null,
    }
}

/// `String(value)` for a macro value.
fn js_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "null".into(),
        Value::Number(n) => n
            .as_f64()
            .and_then(bd_domain::jsout::number_to_string)
            .unwrap_or_default(),
        other => other.to_string(),
    }
}

/// `expandMacros`: one pass, every `${name}` occurrence, in macro declaration order.
fn expand_macros(cmd: &str, macros: &Map<String, Value>) -> String {
    let mut expanded = cmd.to_string();
    for (name, value) in macros {
        expanded = expanded.replace(&format!("${{{name}}}"), &js_string(value));
    }
    expanded
}

/// `tokenizeCmd`: `split(/\s+/)` without empties.
pub fn tokenize_cmd(cmd: &str) -> Vec<&str> {
    cmd.split(is_js_space).filter(|t| !t.is_empty()).collect()
}

fn value_after<'a>(tokens: &[&'a str], flag: &str) -> Option<&'a str> {
    let i = tokens.iter().position(|t| *t == flag)?;
    tokens.get(i + 1).copied()
}

/// `toIntFlag`: `/^\d+$/` then `parseInt`.
fn to_int_flag(raw: Option<&str>) -> Option<f64> {
    let raw = raw?;
    if raw.is_empty() || !raw.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    raw.parse::<f64>().ok()
}

/// `extractRecipe`: the recipe flags of a macro-expanded `cmd`; KV types default to `f16`.
pub fn extract_recipe(cmd: &str) -> ServingRecipe {
    let tokens = tokenize_cmd(cmd);
    let after = |f: &str| value_after(&tokens, f).map(str::to_string);
    ServingRecipe {
        model_path: after("-m"),
        draft_path: after("-md"),
        spec_type: after("--spec-type"),
        moe_cpu_layers: to_int_flag(value_after(&tokens, "-ncmoe")),
        kv_k: after("-ctk").unwrap_or_else(|| "f16".into()),
        kv_v: after("-ctv").unwrap_or_else(|| "f16".into()),
    }
}

/// `Bun.file(path).text()`: an unreadable file is the routes' 502 (runtime I/O text).
fn read_config_text(path: &str) -> Result<String, String> {
    let bytes = std::fs::read(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            format!("ENOENT: no such file or directory, open '{path}'")
        } else {
            format!("{e}, open '{path}'")
        }
    })?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

// ---- GET /api/serving: the rest of llamaConfig.ts -----------------------------------------

/// `FLAG_TOKEN`: `/^--?[a-zA-Z]/`.
pub fn is_flag_token(t: &str) -> bool {
    let rest = t.strip_prefix("--").or_else(|| t.strip_prefix('-'));
    rest.and_then(|r| r.bytes().next())
        .is_some_and(|c| c.is_ascii_alphabetic())
}

/// `ServingFlagEntry`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct FlagEntry {
    pub flag: String,
    pub value: Option<String>,
}

/// `extractAllFlags`.
pub fn extract_all_flags(tokens: &[&str]) -> Vec<FlagEntry> {
    let Some(first) = tokens.iter().position(|t| is_flag_token(t)) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let mut i = first;
    while i < tokens.len() {
        let flag = tokens[i];
        if !is_flag_token(flag) {
            i += 1;
            continue;
        }
        match tokens.get(i + 1) {
            Some(next) if !is_flag_token(next) => {
                out.push(FlagEntry {
                    flag: flag.into(),
                    value: Some((*next).into()),
                });
                i += 2;
            }
            _ => {
                out.push(FlagEntry {
                    flag: flag.into(),
                    value: None,
                });
                i += 1;
            }
        }
    }
    out
}

/// `ServingFlags` (`extractFlags`).
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServingFlags {
    pub context_length: Option<f64>,
    pub kv_cache_type_k: Option<String>,
    pub kv_cache_type_v: Option<String>,
    pub flash_attention: Option<String>,
    pub gpu_layers: Option<f64>,
    pub all_flags: Vec<FlagEntry>,
}

/// `extractFlags`.
pub fn extract_flags(cmd: &str) -> ServingFlags {
    let tokens = tokenize_cmd(cmd);
    let after = |f: &str| value_after(&tokens, f).map(str::to_string);
    ServingFlags {
        context_length: to_int_flag(value_after(&tokens, "-c")),
        kv_cache_type_k: after("-ctk"),
        kv_cache_type_v: after("-ctv"),
        flash_attention: after("-fa"),
        gpu_layers: to_int_flag(value_after(&tokens, "-ngl")),
        all_flags: extract_all_flags(&tokens),
    }
}

/// A JS property read on an arbitrary value: only objects carry the config's keys (a string
/// or array has none of them), and an absent key is `undefined` (None).
fn prop<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    v.as_object().and_then(|m| m.get(key))
}

/// `x ?? fallback`: `undefined` and `null` both fall through.
fn nn(v: Option<&Value>, fallback: Value) -> Value {
    match v {
        None | Some(Value::Null) => fallback,
        Some(x) => x.clone(),
    }
}

fn type_error(what: &str) -> String {
    format!("TypeError: null is not an object (evaluating '{what}')")
}

/// `extractCapability`: field-by-field, values carried through as the YAML has them (the
/// reader's contract is "what the YAML says"; `check:recipes` owns validation).
fn extract_capability(metadata: Option<&Value>) -> Result<Value, String> {
    let Some(md) = metadata else {
        return Ok(Value::Null);
    };
    if md.is_null() {
        return Err(type_error("metadata.serving"));
    }
    let serving = nn(prop(md, "serving"), Value::Object(Map::new()));
    let s = |k: &str| nn(prop(&serving, k), Value::Null);
    let m = |k: &str| nn(prop(md, k), Value::Null);
    let sampling = match prop(md, "sampling") {
        None => Value::Null,
        Some(Value::Null) => return Err(type_error("metadata.sampling.temperature")),
        Some(sp) => {
            let g = |k: &str| nn(prop(sp, k), Value::Null);
            serde_json::json!({
                "temperature": g("temperature"),
                "topP": g("top_p"),
                "topK": g("top_k"),
                "minP": g("min_p"),
                "presencePenalty": g("presence_penalty"),
                "repetitionPenalty": g("repetition_penalty"),
            })
        }
    };
    let max_output = match prop(md, "max_output") {
        None => Value::Null,
        Some(Value::Null) => return Err(type_error("metadata.max_output.reasoning_budget")),
        Some(mo) => serde_json::json!({
            "reasoningBudget": nn(prop(mo, "reasoning_budget"), Value::Null),
            "answerTokens": nn(prop(mo, "answer_tokens"), Value::Null),
        }),
    };
    Ok(serde_json::json!({
        "contextLength": m("context_length"),
        "inputModalities": nn(prop(md, "input_modalities"), Value::Array(Vec::new())),
        "outputModalities": nn(prop(md, "output_modalities"), Value::Array(Vec::new())),
        "toolCalling": m("tool_calling"),
        "reasoning": m("reasoning"),
        "serving": {
            "quant": s("quant"),
            "kvCacheType": s("kv_cache_type"),
            "flashAttention": s("flash_attention"),
            "mtp": s("mtp"),
            "moeCpuOffloadLayers": s("moe_cpu_offload_layers"),
            "ubatch": s("ubatch"),
            "reasoningMode": s("reasoning_mode"),
            "reasoningBudget": s("reasoning_budget"),
        },
        "sampling": sampling,
        "maxOutput": max_output,
        "sources": m("sources"),
    }))
}

/// `modelAliases.length === 0` on whatever `aliases ?? []` holds (a non-array, non-string
/// value has no `length`, and `undefined === 0` is false).
fn js_length_is_zero(v: &Value) -> bool {
    match v {
        Value::Array(a) => a.is_empty(),
        Value::String(s) => s.is_empty(),
        _ => false,
    }
}

/// `parseServedAliases` in full: the `ServingAlias` wire objects, in `Object.entries` order.
pub fn parse_served_aliases(text: &str) -> Result<Vec<Value>, String> {
    let doc = yaml_parse(text)?;
    if doc.is_null() {
        return Err(type_error("doc.macros"));
    }
    let empty = Map::new();
    let field = |name: &str| {
        prop(&doc, name)
            .and_then(Value::as_object)
            .unwrap_or(&empty)
    };
    let (macros, models) = (field("macros"), field("models"));
    let macros: Map<String, Value> = crate::jsrt::js_entries(macros)
        .into_iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let mut out = Vec::new();
    for (model_key, model) in crate::jsrt::js_entries(models) {
        if model.is_null() {
            return Err(type_error("model.aliases"));
        }
        let aliases = nn(prop(model, "aliases"), Value::Array(Vec::new()));
        if js_length_is_zero(&aliases) {
            continue;
        }
        let cmd = match prop(model, "cmd") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(s)) => s.clone(),
            Some(_) => return Err("TypeError: cmd.split is not a function".into()),
        };
        let expanded = expand_macros(&cmd, &macros);
        let ttl = match prop(model, "ttl") {
            Some(Value::Number(n)) => Value::Number(n.clone()),
            _ => Value::Null,
        };
        out.push(serde_json::json!({
            "modelKey": model_key,
            "name": nn(prop(model, "name"), Value::Null),
            "aliases": aliases,
            "ttl": ttl,
            "checkEndpoint": nn(prop(model, "checkEndpoint"), Value::Null),
            "flags": serde_json::to_value(extract_flags(&expanded)).map_err(|e| e.to_string())?,
            "capability": extract_capability(prop(model, "metadata"))?,
            "recipe": serde_json::to_value(extract_recipe(&expanded)).map_err(|e| e.to_string())?,
        }));
    }
    Ok(out)
}

/// `ServingResponse`.
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServingResponse {
    pub config_path: String,
    pub models: Vec<Value>,
}

/// `readServingConfig`: read per request, never cached, never written.
pub fn read_serving_config(path: &str) -> Result<ServingResponse, String> {
    let models = parse_served_aliases(&read_config_text(path)?)?;
    Ok(ServingResponse {
        config_path: path.to_string(),
        models,
    })
}

#[cfg(test)]
#[path = "serving_tests.rs"]
mod tests;
