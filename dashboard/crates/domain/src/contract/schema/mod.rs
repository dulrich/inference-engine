//! `packages/shared/src/*.ts` as contract data, one module per source file. Each `Export`
//! carries the zod schema's name minus the `Schema` suffix (`tsgen` adds it back).
use super::*;

mod health;
mod router_status;
mod serving;
mod serving_facts;
mod serving_live;
mod serving_request;

pub use health::*;
pub use router_status::*;
pub use serving::*;
pub use serving_facts::*;
pub use serving_live::*;
pub use serving_request::*;

const fn f(name: &'static str, node: &'static Node) -> Field {
    Field { name, node }
}

const fn obj(fields: &'static [Field]) -> Node {
    Node::Object {
        fields,
        unknown: Unknown::Strip,
    }
}

const fn passthrough(fields: &'static [Field]) -> Node {
    Node::Object {
        fields,
        unknown: Unknown::Passthrough,
    }
}

const STR: Node = Node::Str(StrRules::ANY);
const NULL_STR: Node = Node::Nullable(&STR);
const NULLISH_STR: Node = Node::Optional(&NULL_STR);
const STR_MIN1: Node = Node::Str(StrRules::ANY.min(1));
const NUM: Node = Node::Num(NumRules::ANY);
const NULL_NUM: Node = Node::Nullable(&NUM);
const NULLISH_NUM: Node = Node::Optional(&NULL_NUM);
const INT: Node = Node::Num(NumRules::ANY.int());
const NULL_INT: Node = Node::Nullable(&INT);
const NONNEG_INT: Node = Node::Num(NumRules::ANY.int().gte(0.0));
const POS_INT: Node = Node::Num(NumRules::ANY.int().gt(0.0));
const BOOL: Node = Node::Bool;
const NULL_BOOL: Node = Node::Nullable(&BOOL);
const UNKNOWN: Node = Node::Unknown;
const STR_LIST: Node = Node::Array(&STR);
const UNKNOWN_RECORD: Node = Node::Record {
    key: &STR,
    value: &UNKNOWN,
};
