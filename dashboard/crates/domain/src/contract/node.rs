//! The contract's node vocabulary: string/number rules, literals, defaults, regex patterns,
//! [`Node`], [`Export`] and exported constants. Base: `radar_tracker/crates/domain/src/contract/
//! node.rs` (plan Decision 5). Added for this contract (Decision 5 + audit E1): `z.null()`,
//! `z.literal` (string, number, boolean), `.default(...)` and the ordinary `z.union` (ported from
//! `subscription_tracker/crates/domain/src/contract/node.rs`); string `.min(n, message?)` and
//! `.regex(pattern, message?)` (subscription's `StrRules`, with patterns as data instead of a
//! closed civil-date enum); `z.discriminatedUnion`; `.passthrough()` objects. Radar's coerce,
//! strict objects and empty-string preprocess are dropped (unused here).
use serde_json::Value;

/// A JS regex literal used by `.regex(...)` and, when `export` is set, exported verbatim as
/// `export const <name> = /<source>/;`.
#[derive(Debug)]
pub struct Pattern {
    pub name: &'static str,
    /// The JS regex source (between the slashes). ASCII-only, no flags.
    pub source: &'static str,
}

/// `z.string()` checks, in zod's order: `.min`, then `.regex`. Each carries zod's optional
/// custom message (`None` = zod's default message).
#[derive(Clone, Copy, Debug)]
pub struct StrRules {
    pub min: Option<(u32, Option<&'static str>)>,
    pub regex: Option<(&'static Pattern, Option<&'static str>)>,
}

impl StrRules {
    pub const ANY: StrRules = StrRules {
        min: None,
        regex: None,
    };
    pub const fn min(mut self, n: u32) -> Self {
        self.min = Some((n, None));
        self
    }
    pub const fn min_msg(mut self, n: u32, message: &'static str) -> Self {
        self.min = Some((n, Some(message)));
        self
    }
    pub const fn regex(mut self, p: &'static Pattern) -> Self {
        self.regex = Some((p, None));
        self
    }
    pub const fn regex_msg(mut self, p: &'static Pattern, message: &'static str) -> Self {
        self.regex = Some((p, Some(message)));
        self
    }
}

/// `z.number()` checks: `.int()`, `.positive()` (`gt 0`), `.nonnegative()`/`.min(n)` (`gte`),
/// `.max(n)` (`lte`). NaN is never a number.
#[derive(Clone, Copy, Debug)]
pub struct NumRules {
    pub int: bool,
    pub gt: Option<f64>,
    pub gte: Option<f64>,
    pub lte: Option<f64>,
}

impl NumRules {
    pub const ANY: NumRules = NumRules {
        int: false,
        gt: None,
        gte: None,
        lte: None,
    };
    pub const fn int(mut self) -> Self {
        self.int = true;
        self
    }
    pub const fn gt(mut self, n: f64) -> Self {
        self.gt = Some(n);
        self
    }
    pub const fn gte(mut self, n: f64) -> Self {
        self.gte = Some(n);
        self
    }
    pub const fn lte(mut self, n: f64) -> Self {
        self.lte = Some(n);
        self
    }
}

/// A `z.literal(...)` value.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Lit {
    Str(&'static str),
    Num(f64),
    Bool(bool),
}

impl Lit {
    pub fn value(self) -> Value {
        match self {
            Lit::Str(s) => Value::String(s.into()),
            Lit::Num(n) => crate::js::json_number(n),
            Lit::Bool(b) => Value::Bool(b),
        }
    }
    /// The literal as JS source text (also its TS type).
    pub fn js(self) -> String {
        match self {
            Lit::Str(s) => serde_json::to_string(s).unwrap_or_default(),
            Lit::Num(n) => crate::jsout::number_to_string(n).unwrap_or_default(),
            Lit::Bool(b) => b.to_string(),
        }
    }
}

/// A `.default(...)` value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DefaultValue {
    Null,
    EmptyArray,
}

impl DefaultValue {
    pub fn value(self) -> Value {
        match self {
            DefaultValue::Null => Value::Null,
            DefaultValue::EmptyArray => Value::Array(Vec::new()),
        }
    }
    /// The JS source the TS runtime evaluates (a fresh value per parse).
    pub fn js(self) -> &'static str {
        match self {
            DefaultValue::Null => "null",
            DefaultValue::EmptyArray => "[]",
        }
    }
}

/// What a `z.object` does with keys outside its shape.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Unknown {
    /// zod's default: dropped from the output.
    Strip,
    /// `.passthrough()`: kept, after the shape keys, in input key order.
    Passthrough,
}

#[derive(Debug)]
pub struct Field {
    pub name: &'static str,
    pub node: &'static Node,
}

#[derive(Debug)]
pub enum Node {
    Str(StrRules),
    Num(NumRules),
    Bool,
    /// `z.null()`.
    Null,
    /// `z.unknown()`: any value, passed through.
    Unknown,
    /// `z.literal(...)`.
    Lit(Lit),
    /// `z.enum([...])`.
    Enum(&'static [&'static str]),
    Array(&'static Node),
    /// `z.object` (output in field order, then passthrough keys).
    Object {
        fields: &'static [Field],
        unknown: Unknown,
    },
    /// `z.record(key, value)`.
    Record {
        key: &'static Node,
        value: &'static Node,
    },
    Nullable(&'static Node),
    /// `.optional()`: an absent key is skipped (inside objects).
    Optional(&'static Node),
    /// `.default(value)`: an absent value parses the default.
    Default(&'static Node, DefaultValue),
    /// `z.union`: first valid option wins, else the first dirty one, else `invalid_union`.
    Union(&'static [&'static Node]),
    /// `z.discriminatedUnion(key, options)`: every option is an object (possibly via `Ref`)
    /// whose `key` field is a literal; the input's `key` value selects the option.
    DiscUnion {
        key: &'static str,
        options: &'static [&'static Node],
    },
    /// Another exported schema (emitted by name in TS).
    Ref(&'static Export),
}

impl Node {
    /// Calls `visit` on each direct child node; a `Ref` has none (its target is another export).
    pub fn for_each_child(&self, visit: &mut dyn FnMut(&'static Node)) {
        match self {
            Node::Array(n) | Node::Nullable(n) | Node::Optional(n) | Node::Default(n, _) => {
                visit(n)
            }
            Node::Record { key, value } => {
                visit(key);
                visit(value);
            }
            Node::Object { fields, .. } => fields.iter().for_each(|f| visit(f.node)),
            Node::Union(options) | Node::DiscUnion { options, .. } => {
                options.iter().for_each(|o| visit(o))
            }
            Node::Str(_)
            | Node::Num(_)
            | Node::Bool
            | Node::Null
            | Node::Unknown
            | Node::Lit(_)
            | Node::Enum(_)
            | Node::Ref(_) => {}
        }
    }

    /// This node with any `Ref` chain resolved.
    pub fn peel(&self) -> &Node {
        match self {
            Node::Ref(e) => e.node.peel(),
            n => n,
        }
    }

    /// For a discriminated-union option: its object fields.
    pub fn object_fields(&self) -> Option<&'static [Field]> {
        match self.peel() {
            Node::Object { fields, .. } => Some(fields),
            _ => None,
        }
    }
}

/// An exported schema: the `<name>Schema` validator and `type <name>` (`z.infer`). `exported:
/// false` is a module-private schema whose inferred type is still exported (`RouterUnavailable`).
#[derive(Debug)]
pub struct Export {
    pub name: &'static str,
    pub node: Node,
    pub exported: bool,
}

impl Export {
    pub(super) const fn out(name: &'static str, node: Node) -> Export {
        Export {
            name,
            node,
            exported: true,
        }
    }
    pub(super) const fn private(name: &'static str, node: Node) -> Export {
        Export {
            name,
            node,
            exported: false,
        }
    }
    /// The validator's exported name (zod source naming).
    pub fn schema_name(&self) -> String {
        format!("{}Schema", self.name)
    }
}

/// An export validates as its node (`validate::parse(&contract::HEALTH_RESPONSE, ..)`).
impl std::ops::Deref for Export {
    type Target = Node;
    fn deref(&self) -> &Node {
        &self.node
    }
}

/// The value of an exported (non-schema) constant.
#[derive(Debug)]
pub enum ConstValue {
    Str(&'static str),
    /// An integer constant (`export const X = 15;`).
    Int(i64),
    Regex(&'static Pattern),
    /// A typed string list: `export const X: readonly <ty>[] = ["a", "b"];`.
    List {
        ty: &'static str,
        items: &'static [&'static str],
    },
}

/// An `export const` of the contract module, emitted verbatim by `gen-ts`.
#[derive(Debug)]
pub struct Const {
    pub name: &'static str,
    pub value: ConstValue,
}
