//! Emits the `@bench-dashboard/shared` contract module from `contract`: the exported constants,
//! the node tree as data, and `RUNTIME`, a rule-free interpreter of it that mirrors `validate`
//! (zod v3 parse semantics: valid/dirty/aborted, strip or passthrough, field order, unions,
//! discriminated unions, defaults). Copied from radar_tracker's `tsgen.rs` (plan Decision 5) and
//! extended with this contract's vocabulary. Exports keep the zod sources' naming
//! (`<name>Schema` validators, `<name>` types, constants verbatim). Until WP-6 the output goes to
//! a scratch path (`target/contract-gen/index.ts`); zod stays the live contract.
use crate::contract::{ConstValue, Export, Node, CONSTS, EXPORTS};
use std::fmt::Write;

/// The generated module's rule-free interpreter, emitted verbatim after the constants.
pub const RUNTIME: &str = include_str!("runtime.ts");

fn quote(s: &str) -> String {
    serde_json::to_string(s).unwrap_or_default()
}

fn quote_list(v: &[&str], sep: &str) -> String {
    v.iter().map(|s| quote(s)).collect::<Vec<_>>().join(sep)
}

fn paren(t: String) -> String {
    if t.contains('|') || t.contains('&') {
        format!("({t})")
    } else {
        t
    }
}

/// Whether an object member of this node may be absent: `z.infer` adds `?` when `undefined` is
/// assignable to the member type (`.optional()`, and `z.unknown()` even when nullable).
fn may_be_absent(node: &Node) -> bool {
    match node.peel() {
        Node::Optional(_) | Node::Unknown => true,
        Node::Nullable(inner) => matches!(inner.peel(), Node::Unknown),
        _ => false,
    }
}

fn ts_type(node: &Node) -> String {
    match node {
        Node::Str(_) => "string".into(),
        Node::Num(_) => "number".into(),
        Node::Bool => "boolean".into(),
        Node::Null => "null".into(),
        Node::Unknown => "unknown".into(),
        Node::Lit(l) => l.js(),
        Node::Enum(v) => quote_list(v, " | "),
        Node::Array(item) => format!("{}[]", paren(ts_type(item))),
        Node::Object { fields, unknown } => {
            let parts: Vec<String> = fields
                .iter()
                .map(|f| {
                    let name = if f
                        .name
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || c == '_')
                    {
                        f.name.to_string()
                    } else {
                        quote(f.name)
                    };
                    let q = if may_be_absent(f.node) { "?" } else { "" };
                    format!("{name}{q}: {}", ts_type(f.node))
                })
                .collect();
            let shape = format!("{{ {} }}", parts.join("; "));
            match unknown {
                crate::contract::Unknown::Strip => shape,
                crate::contract::Unknown::Passthrough => {
                    format!("{shape} & {{ [k: string]: unknown }}")
                }
            }
        }
        Node::Record { value, .. } => format!("Record<string, {}>", ts_type(value)),
        Node::Nullable(inner) => format!("{} | null", ts_type(inner)),
        Node::Optional(inner) => format!("{} | undefined", ts_type(inner)),
        Node::Default(inner, _) => ts_type(inner),
        Node::Union(options) | Node::DiscUnion { options, .. } => options
            .iter()
            .map(|o| paren(ts_type(o)))
            .collect::<Vec<_>>()
            .join(" | "),
        Node::Ref(e) => e.name.to_string(),
    }
}

fn opt_num<T: std::fmt::Display>(v: Option<T>) -> String {
    v.map_or("null".into(), |n| n.to_string())
}

fn opt_quote(v: Option<&str>) -> String {
    v.map_or("null".into(), quote)
}

fn node_list(nodes: &[&Node]) -> String {
    nodes
        .iter()
        .map(|n| node_js(n))
        .collect::<Vec<_>>()
        .join(", ")
}

fn node_js(node: &Node) -> String {
    match node {
        Node::Str(r) => format!(
            r#"{{ k: "str", min: {}, minMsg: {}, re: {}, reMsg: {} }}"#,
            opt_num(r.min.map(|(n, _)| n)),
            opt_quote(r.min.and_then(|(_, m)| m)),
            r.regex.map_or("null", |(p, _)| p.name),
            opt_quote(r.regex.and_then(|(_, m)| m)),
        ),
        Node::Num(r) => format!(
            r#"{{ k: "num", int: {}, gt: {}, gte: {}, lte: {} }}"#,
            r.int,
            opt_num(r.gt),
            opt_num(r.gte),
            opt_num(r.lte)
        ),
        Node::Bool => r#"{ k: "bool" }"#.into(),
        Node::Null => r#"{ k: "null" }"#.into(),
        Node::Unknown => r#"{ k: "unknown" }"#.into(),
        Node::Lit(l) => format!(r#"{{ k: "lit", value: {} }}"#, l.js()),
        Node::Enum(v) => format!(r#"{{ k: "enum", values: [{}] }}"#, quote_list(v, ", ")),
        Node::Array(item) => format!(r#"{{ k: "array", item: {} }}"#, node_js(item)),
        Node::Object { fields, unknown } => {
            let fs: Vec<String> = fields
                .iter()
                .map(|f| {
                    format!(
                        r#"{{ name: {}, node: {} }}"#,
                        quote(f.name),
                        node_js(f.node)
                    )
                })
                .collect();
            format!(
                r#"{{ k: "object", fields: [{}], passthrough: {} }}"#,
                fs.join(", "),
                *unknown == crate::contract::Unknown::Passthrough
            )
        }
        Node::Record { key, value } => format!(
            r#"{{ k: "record", key: {}, value: {} }}"#,
            node_js(key),
            node_js(value)
        ),
        Node::Nullable(inner) => format!(r#"{{ k: "nullable", inner: {} }}"#, node_js(inner)),
        Node::Optional(inner) => format!(r#"{{ k: "optional", inner: {} }}"#, node_js(inner)),
        Node::Default(inner, d) => format!(
            r#"{{ k: "default", inner: {}, make: () => {} }}"#,
            node_js(inner),
            d.js()
        ),
        Node::Union(options) => format!(r#"{{ k: "union", options: [{}] }}"#, node_list(options)),
        Node::DiscUnion { key, options } => format!(
            r#"{{ k: "disc", key: {}, options: [{}] }}"#,
            quote(key),
            node_list(options)
        ),
        Node::Ref(e) => format!("N_{}", e.name),
    }
}

fn constant(out: &mut String, name: &str, value: &ConstValue) {
    let _ = match value {
        ConstValue::Str(s) => writeln!(out, "export const {name} = {};", quote(s)),
        ConstValue::Int(n) => writeln!(out, "export const {name} = {n};"),
        ConstValue::Regex(p) => writeln!(out, "export const {name} = /{}/;", p.source),
        ConstValue::List { ty, items } => writeln!(
            out,
            "export const {name}: readonly {ty}[] = [{}];",
            quote_list(items, ", ")
        ),
    };
}

fn export(out: &mut String, e: &Export) {
    let schema = e.schema_name();
    let _ = writeln!(out, "const N_{}: Node = {};", e.name, node_js(&e.node));
    let _ = writeln!(out, "export type {} = {};", e.name, ts_type(&e.node));
    if !e.exported {
        return;
    }
    if let Node::Enum(values) = &e.node {
        let _ = writeln!(
            out,
            "export const {schema} = enumValidator<{n}, [{opts}]>(N_{n} as Node & {{ k: \"enum\" }});",
            n = e.name,
            opts = quote_list(values, ", ")
        );
    } else {
        let _ = writeln!(
            out,
            "export const {schema}: Validator<{n}> = validator<{n}>(N_{n});",
            n = e.name
        );
    }
}

pub fn module() -> String {
    let mut out = String::new();
    out.push_str(
        "// GENERATED by bench-dashboard `gen-ts` from crates/domain/src/contract/. Do not edit.\n",
    );
    for c in CONSTS {
        constant(&mut out, c.name, &c.value);
    }
    out.push_str(RUNTIME);
    for e in EXPORTS {
        export(&mut out, e);
    }
    out
}

#[cfg(test)]
mod tests {
    use crate::contract::{Node, EXPORTS};

    fn refs(node: &Node, out: &mut Vec<&'static str>) {
        if let Node::Ref(e) = node {
            out.push(e.name);
        }
        node.for_each_child(&mut |n| refs(n, out));
    }

    /// The emitted module declares `const N_<name>` in `EXPORTS` order, so a `Ref` target must
    /// be exported, under a unique name, before its first user.
    #[test]
    fn ref_targets_precede_their_users() {
        let index = |name: &str| EXPORTS.iter().position(|e| e.name == name);
        for (i, e) in EXPORTS.iter().enumerate() {
            assert_eq!(index(e.name), Some(i), "{} exported twice", e.name);
            let mut used = Vec::new();
            refs(&e.node, &mut used);
            for name in used {
                let at =
                    index(name).unwrap_or_else(|| panic!("{}: Ref {name} is not exported", e.name));
                assert!(at < i, "{}: Ref {name} is exported after its user", e.name);
            }
        }
    }

    /// `llama/bakeoff/tests/test_control_contract.py:184` string-asserts this exact line.
    #[test]
    fn control_request_schema_constant_is_verbatim() {
        let module = super::module();
        assert!(
            module.contains("\nexport const CONTROL_REQUEST_SCHEMA_V1 = \"control-request-v1\";\n")
        );
        assert!(module
            .contains("\nexport const CONTROL_QUEUE_STEM_PATTERN = /^[a-z0-9][a-z0-9-]*$/;\n"));
        assert!(module.contains("\nexport const SERVING_STATUS_STALE_MS = 180000;\n"));
    }

    /// Every discriminated-union option is an object carrying a literal for the key, and no
    /// two options share one.
    #[test]
    fn discriminated_union_options_have_distinct_literals() {
        for e in EXPORTS {
            if let Node::DiscUnion { key, options } = &e.node {
                let mut seen = Vec::new();
                for o in options.iter() {
                    let field = o
                        .object_fields()
                        .and_then(|fs| fs.iter().find(|f| f.name == *key))
                        .unwrap_or_else(|| panic!("{}: option without `{key}`", e.name));
                    let Node::Lit(l) = field.node.peel() else {
                        panic!("{}: `{key}` is not a literal", e.name);
                    };
                    assert!(!seen.contains(l), "{}: duplicate tag", e.name);
                    seen.push(*l);
                }
            }
        }
    }
}
