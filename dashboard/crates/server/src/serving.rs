//! The served-recipe join: `apps/api/src/serving/recipe.ts` in full and the part of
//! `serving/llamaConfig.ts` the record-derived routes need (`readServingConfig` ->
//! `parseServedAliases`' model key, aliases and `extractRecipe` over the macro-expanded
//! `cmd`). `GET /api/serving`'s full projection (`flags`, `capability`, `name`, `ttl`,
//! `checkEndpoint`) is WP-4's, on top of this reader. Read per request, never cached, never
//! written; YAML through `yaml-rust2` with map order preserved (plan Spike 1).
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

/// The `ServingAlias` subset the join reads.
#[derive(Debug, Clone, PartialEq)]
pub struct ServedModel {
    pub model_key: String,
    pub aliases: Vec<String>,
    pub cmd: String,
    pub recipe: ServingRecipe,
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

/// `projectRecipe`.
fn project(
    spec_type: Option<String>,
    draft_path: Option<String>,
    moe_cpu_layers: Option<f64>,
    kv_k: Option<String>,
    kv_v: Option<String>,
) -> (bool, ServingRecipe) {
    let mtp = spec_type.is_some() || draft_path.is_some();
    (
        mtp,
        ServingRecipe {
            model_path: None,
            draft_path,
            spec_type,
            moe_cpu_layers,
            kv_k: kv_k.unwrap_or_else(|| "f16".into()),
            kv_v: kv_v.unwrap_or_else(|| "f16".into()),
        },
    )
}

/// `extractRecipe`.
pub fn extract_recipe(cmd: &str) -> ServingRecipe {
    let tokens = tokenize_cmd(cmd);
    let after = |f: &str| value_after(&tokens, f).map(str::to_string);
    let (_, mut recipe) = project(
        after("--spec-type"),
        after("-md"),
        to_int_flag(value_after(&tokens, "-ncmoe")),
        after("-ctk"),
        after("-ctv"),
    );
    recipe.model_path = after("-m");
    recipe
}

/// `parseServedAliases`' served-model list (models publishing at least one alias).
pub fn parse_served_models(text: &str) -> Result<Vec<ServedModel>, String> {
    let doc = yaml_parse(text)?;
    if doc.is_null() {
        return Err("null is not an object (evaluating 'doc.macros')".into());
    }
    let empty = Map::new();
    let field = |name: &str| doc.get(name).and_then(Value::as_object).unwrap_or(&empty);
    let (macros, models) = (field("macros"), field("models"));
    let mut out = Vec::new();
    for (model_key, model) in models {
        let aliases: Vec<String> = model
            .get("aliases")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(js_string).collect())
            .unwrap_or_default();
        if aliases.is_empty() {
            continue;
        }
        let cmd = expand_macros(
            model.get("cmd").and_then(Value::as_str).unwrap_or(""),
            macros,
        );
        out.push(ServedModel {
            model_key: model_key.clone(),
            aliases,
            recipe: extract_recipe(&cmd),
            cmd,
        });
    }
    Ok(out)
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

/// `readServingConfig(path).models`: an unreadable file or a YAML error is the routes' 502.
pub fn read_served_models(path: &str) -> Result<Vec<ServedModel>, String> {
    parse_served_models(&read_config_text(path)?)
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

// ---- recipe.ts --------------------------------------------------------------------------

/// The five-field recipe equality tuple (`Recipe`); `model_path` already normalized.
#[derive(Debug, Clone, PartialEq)]
pub struct Recipe {
    pub model_path: String,
    pub mtp: bool,
    pub moe_cpu_layers: Option<f64>,
    pub kv_k: String,
    pub kv_v: String,
}

/// `FlagRecipe`.
#[derive(Debug, Clone, PartialEq)]
pub struct FlagRecipe {
    pub mtp: bool,
    pub spec_type: Option<String>,
    pub moe_cpu_layers: Option<f64>,
    pub kv_k: String,
    pub kv_v: String,
}

const STALE_MODEL_PREFIX: &str = "/srv/models/";
const CURRENT_MODEL_PREFIX: &str = "/models/";

/// `normalizeModelPath`.
pub fn normalize_model_path(p: &str) -> String {
    match p.strip_prefix(STALE_MODEL_PREFIX) {
        Some(rest) => format!("{CURRENT_MODEL_PREFIX}{rest}"),
        None => p.to_string(),
    }
}

/// `decodeServerFlags`: `Ok(None)` for the null column, `Err(())` for INVALID_SERVER_FLAGS.
#[allow(clippy::result_unit_err)]
pub fn decode_server_flags(raw: Option<&str>) -> Result<Option<Map<String, Value>>, ()> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    match crate::jsrt::json_parse(raw) {
        Ok(Value::Object(m)) => Ok(Some(m)),
        _ => Err(()),
    }
}

/// `recipeFromServerFlags`: None when a recipe-bearing key is present with the wrong type.
pub fn recipe_from_server_flags(flags: Option<&Map<String, Value>>) -> Option<FlagRecipe> {
    let get = |k: &str| flags.and_then(|f| f.get(k));
    // `isOptionalString`: absent or a string (JSON null is present-and-wrong).
    let opt_str = |k: &str| -> Result<Option<String>, ()> {
        match get(k) {
            None => Ok(None),
            Some(Value::String(s)) => Ok(Some(s.clone())),
            Some(_) => Err(()),
        }
    };
    let spec_type = opt_str("--spec-type").ok()?;
    let draft_path = opt_str("-md").ok()?;
    let kv_k = opt_str("-ctk").ok()?;
    let kv_v = opt_str("-ctv").ok()?;
    let moe = match get("-ncmoe") {
        None => None,
        Some(v) => match v.as_f64() {
            Some(n) if n.fract() == 0.0 => Some(n),
            _ => return None,
        },
    };
    let (mtp, r) = project(spec_type, draft_path, moe, kv_k, kv_v);
    Some(FlagRecipe {
        mtp,
        spec_type: r.spec_type,
        moe_cpu_layers: r.moe_cpu_layers,
        kv_k: r.kv_k,
        kv_v: r.kv_v,
    })
}

/// `recipeFromServedAlias`: None without a `-m` path.
pub fn recipe_from_served_model(m: &ServedModel) -> Option<Recipe> {
    let r = &m.recipe;
    let path = r.model_path.as_deref()?;
    let (mtp, p) = project(
        r.spec_type.clone(),
        r.draft_path.clone(),
        r.moe_cpu_layers,
        Some(r.kv_k.clone()),
        Some(r.kv_v.clone()),
    );
    Some(Recipe {
        model_path: normalize_model_path(path),
        mtp,
        moe_cpu_layers: p.moe_cpu_layers,
        kv_k: p.kv_k,
        kv_v: p.kv_v,
    })
}

const LEGACY_TIMING_ALIAS_SUFFIX: &str = "-full";

fn is_v1_timing_baseline(r: &Recipe) -> bool {
    !r.mtp && r.moe_cpu_layers.is_none() && r.kv_k == "f16" && r.kv_v == "f16"
}

/// `ResolvableTimingRow`.
#[derive(Debug, Clone, PartialEq)]
pub struct ResolvableTimingRow {
    pub model_host_path: Option<String>,
    pub sweep_alias: Option<String>,
    pub schema_origin: String,
    pub sweep_server_flags: Option<String>,
}

/// `ResolvedServedModel` — also the capture export's `serving` (`CaptureServing`).
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ResolvedServedModel {
    pub model_key: String,
    pub aliases: Vec<String>,
    pub recipe_match: bool,
    pub mtp: bool,
    pub moe_cpu_layers: Option<f64>,
    pub spec_type: Option<String>,
}

/// `resolveServedModel`: path join, else the v1-timing alias join, else None.
pub fn resolve_served_model(
    row: &ResolvableTimingRow,
    served: &[ServedModel],
) -> Option<ResolvedServedModel> {
    if let Some(path) = &row.model_host_path {
        let normalized = normalize_model_path(path);
        let model = served
            .iter()
            .find(|m| recipe_from_served_model(m).is_some_and(|r| r.model_path == normalized))?;
        let served_recipe = recipe_from_served_model(model)?;
        let flags = decode_server_flags(row.sweep_server_flags.as_deref()).ok()?;
        let flag_recipe = recipe_from_server_flags(flags.as_ref())?;
        let row_recipe = Recipe {
            model_path: normalized,
            mtp: flag_recipe.mtp,
            moe_cpu_layers: flag_recipe.moe_cpu_layers,
            kv_k: flag_recipe.kv_k.clone(),
            kv_v: flag_recipe.kv_v.clone(),
        };
        return Some(ResolvedServedModel {
            model_key: model.model_key.clone(),
            aliases: model.aliases.clone(),
            recipe_match: row_recipe == served_recipe,
            mtp: flag_recipe.mtp,
            moe_cpu_layers: flag_recipe.moe_cpu_layers,
            spec_type: flag_recipe.spec_type,
        });
    }
    if row.schema_origin == "v1-timing" {
        let alias = row.sweep_alias.as_deref()?;
        let joined = alias
            .strip_suffix(LEGACY_TIMING_ALIAS_SUFFIX)
            .unwrap_or(alias);
        let model = served
            .iter()
            .find(|m| m.aliases.iter().any(|a| a == joined))?;
        let served_recipe = recipe_from_served_model(model)?;
        if !is_v1_timing_baseline(&served_recipe) {
            return None;
        }
        return Some(ResolvedServedModel {
            model_key: model.model_key.clone(),
            aliases: model.aliases.clone(),
            recipe_match: true,
            mtp: false,
            moe_cpu_layers: None,
            spec_type: None,
        });
    }
    None
}

#[cfg(test)]
#[path = "serving_tests.rs"]
mod tests;
