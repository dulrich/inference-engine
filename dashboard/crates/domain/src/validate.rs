//! Rust interpreter of the `contract` tree over `serde_json::Value`, with zod v3 parse
//! semantics: every node result is valid, dirty (issues recorded, value still flowing — zod's
//! `status.dirty()`) or aborted. Issues are zod-shaped (`code`, `path`, `message` plus the
//! code's own fields), because the API answers 400s with them. Copied from radar_tracker (plan
//! Decision 5) with radar's coerce/strict/preprocess dropped; union, literal, null, default and
//! string rules ported from subscription_tracker; discriminated unions, passthrough objects and
//! regex patterns as data added for this contract. The generated TS runtime
//! (`tsgen::RUNTIME`) is its twin.
use crate::contract::{Lit, Node, NumRules, Pattern, StrRules, Unknown};
use crate::js;
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

#[derive(Debug, Clone, PartialEq)]
pub enum Seg {
    Key(String),
    Index(usize),
}

#[derive(Debug, Clone, PartialEq)]
pub struct Issue {
    pub code: &'static str,
    pub path: Vec<Seg>,
    pub message: String,
    /// The code's own fields (`expected`/`received`, `minimum`/`inclusive`, `unionErrors`, ...).
    pub extra: Map<String, Value>,
}

fn path_json(path: &[Seg]) -> Value {
    Value::Array(
        path.iter()
            .map(|s| match s {
                Seg::Key(k) => Value::String(k.clone()),
                Seg::Index(i) => json!(i),
            })
            .collect(),
    )
}

impl Issue {
    /// The zod issue object (the `issues` array of a 400 body).
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("code".into(), Value::String(self.code.into()));
        for (k, v) in &self.extra {
            out.insert(k.clone(), v.clone());
        }
        out.insert("path".into(), path_json(&self.path));
        out.insert("message".into(), Value::String(self.message.clone()));
        Value::Object(out)
    }
}

pub fn issues_json(issues: &[Issue]) -> Value {
    Value::Array(issues.iter().map(Issue::to_json).collect())
}

/// `schema.safeParse(input)`.
pub fn parse(node: &Node, input: &Value) -> Result<Value, Vec<Issue>> {
    parse_opt(node, Some(input))
}

/// Why a typed parse failed: the contract rejected the input (a client error, the 400 issues)
/// or the contract's own parsed output does not fit `T` (a server bug: the struct disagrees
/// with the contract node, never a client error).
#[derive(Debug)]
pub enum ParseError {
    Contract(Vec<Issue>),
    Shape(serde_json::Error),
}

/// `schema.parse(input)` typed: the contract applies its zod semantics, then serde types the
/// parsed value.
pub fn parse_into<T: serde::de::DeserializeOwned>(
    node: &Node,
    input: &Value,
) -> Result<T, ParseError> {
    let parsed = parse(node, input).map_err(ParseError::Contract)?;
    serde_json::from_value(parsed).map_err(ParseError::Shape)
}

/// `schema.safeParse(undefined)` when `input` is `None` (an absent key). A top-level skip
/// reads as `null`.
pub fn parse_opt(node: &Node, input: Option<&Value>) -> Result<Value, Vec<Issue>> {
    let mut issues = Vec::new();
    let mut path = Vec::new();
    match run(node, input, &mut path, &mut issues) {
        Some((St::Valid, out)) if issues.is_empty() => Ok(out.value()),
        _ => Err(issues),
    }
}

/// A node's value: a JSON value, or `Skip` for `undefined` (the TS runtime's `SKIP`).
#[derive(Clone)]
enum Out {
    Val(Value),
    Skip,
}

impl Out {
    fn value(self) -> Value {
        match self {
            Out::Val(v) => v,
            Out::Skip => Value::Null,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum St {
    Valid,
    Dirty,
}

/// `None` = aborted.
type R = Option<(St, Out)>;

fn received(v: Option<&Value>) -> &'static str {
    match v {
        None => "undefined",
        Some(Value::Null) => "null",
        Some(Value::Bool(_)) => "boolean",
        Some(Value::Number(_)) => "number",
        Some(Value::String(_)) => "string",
        Some(Value::Array(_)) => "array",
        Some(Value::Object(_)) => "object",
    }
}

fn fmt_num(n: f64) -> String {
    crate::jsout::number_to_string(n).unwrap_or_else(|| n.to_string())
}

fn extra(pairs: Vec<(&str, Value)>) -> Map<String, Value> {
    pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}

struct Cx<'a> {
    path: &'a mut Vec<Seg>,
    issues: &'a mut Vec<Issue>,
}

impl Cx<'_> {
    fn push(&mut self, code: &'static str, message: String, extra: Map<String, Value>) {
        self.issues.push(Issue {
            code,
            path: self.path.clone(),
            message,
            extra,
        });
    }
    fn type_error(&mut self, expected: &str, got: &str, absent: bool) -> R {
        let message = if absent {
            "Required".to_string()
        } else {
            format!("Expected {expected}, received {got}")
        };
        self.push(
            "invalid_type",
            message,
            extra(vec![
                ("expected", json!(expected)),
                ("received", json!(got)),
            ]),
        );
        None
    }
    fn type_error_v(&mut self, expected: &str, v: Option<&Value>) -> R {
        self.type_error(expected, received(v), v.is_none())
    }
    fn too_small(&mut self, ty: &str, min: f64, inclusive: bool, message: String) {
        self.push(
            "too_small",
            message,
            extra(vec![
                ("minimum", js::json_number(min)),
                ("type", json!(ty)),
                ("inclusive", json!(inclusive)),
                ("exact", json!(false)),
            ]),
        );
    }
    fn too_big(&mut self, ty: &str, max: f64, message: String) {
        self.push(
            "too_big",
            message,
            extra(vec![
                ("maximum", js::json_number(max)),
                ("type", json!(ty)),
                ("inclusive", json!(true)),
                ("exact", json!(false)),
            ]),
        );
    }
}

fn valid(v: Value) -> R {
    Some((St::Valid, Out::Val(v)))
}
fn run(node: &Node, v: Option<&Value>, path: &mut Vec<Seg>, issues: &mut Vec<Issue>) -> R {
    let mut cx = Cx { path, issues };
    match node {
        Node::Str(rules) => match v {
            Some(Value::String(s)) => run_str(rules, s, &mut cx),
            _ => cx.type_error_v("string", v),
        },
        Node::Num(rules) => match v {
            Some(Value::Number(n)) => run_num(rules, n.as_f64().unwrap_or(f64::NAN), &mut cx),
            _ => cx.type_error_v("number", v),
        },
        Node::Bool => match v {
            Some(Value::Bool(_)) => valid(v.cloned().unwrap_or(Value::Null)),
            _ => cx.type_error_v("boolean", v),
        },
        Node::Null => match v {
            Some(Value::Null) => valid(Value::Null),
            _ => cx.type_error_v("null", v),
        },
        Node::Unknown => Some((St::Valid, v.map_or(Out::Skip, |x| Out::Val(x.clone())))),
        Node::Lit(lit) => {
            let expected = lit.value();
            if lit_matches(*lit, v) {
                return valid(expected);
            }
            let mut fields = Vec::new();
            if let Some(x) = v {
                fields.push(("received", x.clone()));
            }
            fields.push(("expected", expected));
            cx.push(
                "invalid_literal",
                format!("Invalid literal value, expected {}", lit.js()),
                extra(fields),
            );
            None
        }
        Node::Enum(values) => {
            let options = values
                .iter()
                .map(|s| format!("'{s}'"))
                .collect::<Vec<_>>()
                .join(" | ");
            match v {
                Some(Value::String(s)) if values.contains(&s.as_str()) => valid(json!(s)),
                Some(Value::String(s)) => {
                    cx.push(
                        "invalid_enum_value",
                        format!("Invalid enum value. Expected {options}, received '{s}'"),
                        extra(vec![("received", json!(s)), ("options", json!(values))]),
                    );
                    None
                }
                _ => cx.type_error_v(&options, v),
            }
        }
        Node::Array(item) => {
            let Some(Value::Array(items)) = v else {
                return cx.type_error_v("array", v);
            };
            let mut st = St::Valid;
            let mut aborted = false;
            let mut out = Vec::with_capacity(items.len());
            for (i, x) in items.iter().enumerate() {
                cx.path.push(Seg::Index(i));
                let r = run(item, Some(x), cx.path, cx.issues);
                cx.path.pop();
                match r {
                    Some((s, o)) => {
                        if s == St::Dirty {
                            st = St::Dirty;
                        }
                        out.push(o.value());
                    }
                    None => aborted = true,
                }
            }
            (!aborted).then_some((st, Out::Val(Value::Array(out))))
        }
        Node::Object { fields, unknown } => {
            let Some(Value::Object(src)) = v else {
                return cx.type_error_v("object", v);
            };
            let mut st = St::Valid;
            let mut aborted = false;
            let mut out = Map::new();
            for field in fields.iter() {
                cx.path.push(Seg::Key(field.name.to_string()));
                let r = run(field.node, src.get(field.name), cx.path, cx.issues);
                cx.path.pop();
                match r {
                    Some((s, o)) => {
                        if s == St::Dirty {
                            st = St::Dirty;
                        }
                        if let Out::Val(x) = o {
                            out.insert(field.name.to_string(), x);
                        }
                    }
                    None => aborted = true,
                }
            }
            if *unknown == Unknown::Passthrough {
                // zod v3 `.passthrough()`: keys outside the shape follow it, values verbatim.
                for (k, x) in src {
                    if k != "__proto__" && !fields.iter().any(|f| f.name == k.as_str()) {
                        out.insert(k.clone(), x.clone());
                    }
                }
            }
            (!aborted).then_some((st, Out::Val(Value::Object(out))))
        }
        Node::Record { key, value } => {
            let Some(Value::Object(src)) = v else {
                return cx.type_error_v("object", v);
            };
            let mut st = St::Valid;
            let mut aborted = false;
            let mut out = Map::new();
            for (k, x) in src {
                cx.path.push(Seg::Key(k.clone()));
                let kr = run(key, Some(&Value::String(k.clone())), cx.path, cx.issues);
                let vr = run(value, Some(x), cx.path, cx.issues);
                cx.path.pop();
                match (kr, vr) {
                    (Some((ks, ko)), Some((vs, vo))) => {
                        if ks == St::Dirty || vs == St::Dirty {
                            st = St::Dirty;
                        }
                        // TS parity: `__proto__` is validated but never inserted.
                        if k != "__proto__" {
                            let name = ko.value().as_str().unwrap_or(k).to_string();
                            out.insert(name, vo.value());
                        }
                    }
                    _ => aborted = true,
                }
            }
            (!aborted).then_some((st, Out::Val(Value::Object(out))))
        }
        Node::Nullable(inner) => match v {
            Some(Value::Null) => valid(Value::Null),
            _ => run(inner, v, cx.path, cx.issues),
        },
        Node::Optional(inner) => match v {
            None => Some((St::Valid, Out::Skip)),
            Some(_) => run(inner, v, cx.path, cx.issues),
        },
        Node::Default(inner, default) => match v {
            None => run(inner, Some(&default.value()), cx.path, cx.issues),
            Some(_) => run(inner, v, cx.path, cx.issues),
        },
        // zod v3 `ZodUnion`: each option on a scratch issue list; the first valid result wins,
        // else the first dirty one (its issues kept), else one `invalid_union`.
        Node::Union(options) => {
            let mut dirty: Option<(Out, Vec<Issue>)> = None;
            let mut errors = Vec::new();
            for option in options.iter() {
                let mut scratch = Vec::new();
                match run(option, v, cx.path, &mut scratch) {
                    Some((St::Valid, o)) => return Some((St::Valid, o)),
                    Some((St::Dirty, o)) if dirty.is_none() => {
                        dirty = Some((o, scratch.clone()));
                    }
                    Some((St::Dirty, _)) | None => {}
                }
                if !scratch.is_empty() {
                    errors.push(scratch);
                }
            }
            if let Some((o, found)) = dirty {
                cx.issues.extend(found);
                return Some((St::Dirty, o));
            }
            let union_errors: Vec<Value> = errors
                .iter()
                .map(|issues| json!({"issues": issues_json(issues), "name": "ZodError"}))
                .collect();
            cx.push(
                "invalid_union",
                "Invalid input".into(),
                extra(vec![("unionErrors", Value::Array(union_errors))]),
            );
            None
        }
        // zod v3 `ZodDiscriminatedUnion`: an object whose `key` value selects one option by
        // its literal; an unmatched value is one `invalid_union_discriminator` at `[key]`.
        Node::DiscUnion { key, options } => {
            let Some(Value::Object(src)) = v else {
                return cx.type_error_v("object", v);
            };
            let tags: Vec<(Lit, &Node)> = options
                .iter()
                .filter_map(|o| disc_literal(o, key).map(|l| (l, *o)))
                .collect();
            let found = src.get(*key);
            match tags.iter().find(|(l, _)| lit_matches(*l, found)) {
                Some((_, option)) => run(option, v, cx.path, cx.issues),
                None => {
                    let listed = tags
                        .iter()
                        .map(|(l, _)| match l {
                            Lit::Str(s) => format!("'{s}'"),
                            other => other.js(),
                        })
                        .collect::<Vec<_>>()
                        .join(" | ");
                    let values: Vec<Value> = tags.iter().map(|(l, _)| l.value()).collect();
                    cx.path.push(Seg::Key(key.to_string()));
                    cx.push(
                        "invalid_union_discriminator",
                        format!("Invalid discriminator value. Expected {listed}"),
                        extra(vec![("options", Value::Array(values))]),
                    );
                    cx.path.pop();
                    None
                }
            }
        }
        Node::Ref(export) => run(&export.node, v, cx.path, cx.issues),
    }
}

/// A discriminated-union option's literal for `key`.
fn disc_literal(option: &Node, key: &str) -> Option<Lit> {
    let field = option.object_fields()?.iter().find(|f| f.name == key)?;
    match field.node.peel() {
        Node::Lit(l) => Some(*l),
        _ => None,
    }
}

/// `input === literal` (JS strict equality over the JSON value).
fn lit_matches(lit: Lit, v: Option<&Value>) -> bool {
    match (lit, v) {
        (Lit::Str(s), Some(Value::String(x))) => x == s,
        (Lit::Num(n), Some(Value::Number(x))) => x.as_f64() == Some(n),
        (Lit::Bool(b), Some(Value::Bool(x))) => *x == b,
        _ => false,
    }
}

/// The compiled form of a contract regex. JS patterns here carry no flags and are ASCII-only,
/// so the Rust regex is compiled with Unicode off (`\d` stays `[0-9]`, as in JS without `u`).
pub fn pattern_regex(p: &Pattern) -> &'static regex::Regex {
    static CACHE: OnceLock<Mutex<HashMap<&'static str, &'static regex::Regex>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let mut map = cache.lock().unwrap_or_else(|e| e.into_inner());
    map.entry(p.name).or_insert_with(|| {
        let re = regex::RegexBuilder::new(p.source)
            .unicode(false)
            .build()
            .unwrap_or_else(|e| panic!("contract pattern {} does not compile: {e}", p.name));
        Box::leak(Box::new(re))
    })
}

/// `s.length` in UTF-16 code units, as JS measures it.
fn js_len(s: &str) -> usize {
    s.encode_utf16().count()
}

fn run_str(r: &StrRules, s: &str, cx: &mut Cx) -> R {
    let mut st = St::Valid;
    if let Some((min, message)) = r.min {
        if js_len(s) < min as usize {
            let message = message.map_or_else(
                || format!("String must contain at least {min} character(s)"),
                str::to_string,
            );
            cx.too_small("string", f64::from(min), true, message);
            st = St::Dirty;
        }
    }
    if let Some((p, message)) = r.regex {
        if !pattern_regex(p).is_match(s) {
            cx.push(
                "invalid_string",
                message.unwrap_or("Invalid").to_string(),
                extra(vec![("validation", json!("regex"))]),
            );
            st = St::Dirty;
        }
    }
    Some((st, Out::Val(Value::String(s.to_string()))))
}

fn run_num(r: &NumRules, f: f64, cx: &mut Cx) -> R {
    let mut st = St::Valid;
    if r.int && !(f.is_finite() && f.fract() == 0.0) {
        cx.type_error("integer", "float", false);
        st = St::Dirty;
    }
    if let Some(gt) = r.gt {
        if f <= gt {
            cx.too_small(
                "number",
                gt,
                false,
                format!("Number must be greater than {}", fmt_num(gt)),
            );
            st = St::Dirty;
        }
    }
    if let Some(gte) = r.gte {
        if f < gte {
            cx.too_small(
                "number",
                gte,
                true,
                format!("Number must be greater than or equal to {}", fmt_num(gte)),
            );
            st = St::Dirty;
        }
    }
    if let Some(lte) = r.lte {
        if f > lte {
            cx.too_big(
                "number",
                lte,
                format!("Number must be less than or equal to {}", fmt_num(lte)),
            );
            st = St::Dirty;
        }
    }
    Some((st, Out::Val(js::json_number(f))))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::*;

    fn codes(r: Result<Value, Vec<Issue>>) -> Vec<(&'static str, Value)> {
        r.unwrap_err()
            .iter()
            .map(|i| (i.code, path_json(&i.path)))
            .collect()
    }

    #[test]
    fn passthrough_keeps_and_strip_drops_unknown_keys() {
        let rec = json!({"ts": "t", "kind": "http", "persona": "p", "extra": {"a": 1}});
        assert_eq!(parse(&ROUTER_SIDECAR_RECORD, &rec).unwrap(), rec);
        let persona = json!({"persona": "p", "threadId": null, "model": null, "lastSeenTs": "t",
            "lastKind": "k", "extra": 1});
        let out = parse(&ROUTER_SIDECAR_PERSONA, &persona).unwrap();
        assert!(out.get("extra").is_none());
    }

    #[test]
    fn discriminated_union_selects_by_literal() {
        let off = json!({"available": false, "reason": "disabled", "x": 1});
        assert_eq!(
            parse(&ROUTER_TELEMETRY_RESPONSE, &off).unwrap(),
            json!({"available": false, "reason": "disabled"})
        );
        let bad = parse(&ROUTER_TELEMETRY_RESPONSE, &json!({"available": "yes"})).unwrap_err();
        assert_eq!(
            bad[0].to_json(),
            json!({"code": "invalid_union_discriminator", "options": [true, false],
                "path": ["available"], "message": "Invalid discriminator value. Expected true | false"})
        );
        assert_eq!(
            codes(parse(&ROUTER_TELEMETRY_RESPONSE, &json!([]))),
            vec![("invalid_type", json!([]))]
        );
        assert_eq!(
            codes(parse(
                &ROUTER_TELEMETRY_RESPONSE,
                &json!({"available": false, "reason": "nope"})
            )),
            vec![("invalid_enum_value", json!(["reason"]))]
        );
    }

    #[test]
    fn union_prefers_valid_then_dirty_then_invalid_union() {
        let gap = json!({"url": "none", "reason": "card silent"});
        assert_eq!(parse(&SERVING_SOURCE, &gap).unwrap(), gap);
        // Both options dirty (bad date): the first dirty option's issues surface.
        assert_eq!(
            codes(parse(
                &SERVING_SOURCE,
                &json!({"url": "none", "reason": "r", "verified": "2026"})
            )),
            vec![("invalid_string", json!(["verified"]))]
        );
        let none = parse(&SERVING_SOURCE, &json!({"url": 1})).unwrap_err();
        assert_eq!(none.len(), 1);
        assert_eq!(none[0].code, "invalid_union");
    }

    #[test]
    fn defaults_literals_and_string_rules() {
        let out = parse(
            &SERVING_REQUEST_RECORD,
            &json!({"request_id": "r", "state": "applied", "applied_at": "t"}),
        )
        .unwrap();
        assert_eq!(out["reason"], Value::Null);
        assert_eq!(
            codes(parse(&HEALTH_RESPONSE, &json!({"status": "degraded"}))),
            vec![("invalid_literal", json!(["status"]))]
        );
        let bad = parse(
            &SERVING_VISIBILITY_REQUEST,
            &json!({"modelKey": "", "consumers": []}),
        )
        .unwrap_err();
        assert_eq!(bad[0].code, "too_small");
        assert_eq!(
            bad[0].message,
            "modelKey must be a non-empty llama/config.yaml model key"
        );
        // `\d` is ASCII-only, as in a JS regex without the `u` flag.
        assert!(!pattern_regex(&SERVING_VERIFIED_DATE_PATTERN).is_match("２０２６-01-01"));
        assert!(pattern_regex(&SERVING_VERIFIED_DATE_PATTERN).is_match("2026-01-01"));
        assert!(!pattern_regex(&CONTROL_QUEUE_STEM_PATTERN).is_match("a\n"));
    }
}
