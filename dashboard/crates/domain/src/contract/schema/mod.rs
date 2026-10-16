//! `packages/shared/src/*.ts` as contract data, one module per source file. Each `Export`
//! carries the zod schema's name minus the `Schema` suffix (`tsgen` adds it back).
use super::*;

mod capture;
mod control;
mod curve;
mod experiment;
mod health;
mod ingest_summary;
mod ladder;
mod metric;
mod queue;
mod router_status;
mod serving;
mod serving_request;
mod trial;

pub use capture::*;
pub use control::*;
pub use curve::*;
pub use experiment::*;
pub use health::*;
pub use ingest_summary::*;
pub use ladder::*;
pub use metric::*;
pub use queue::*;
pub use router_status::*;
pub use serving::*;
pub use serving_request::*;
pub use trial::*;

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
const NULL_NONNEG_INT: Node = Node::Nullable(&NONNEG_INT);
const POS_INT: Node = Node::Num(NumRules::ANY.int().gt(0.0));
const BOOL: Node = Node::Bool;
const NULL_BOOL: Node = Node::Nullable(&BOOL);
const UNKNOWN: Node = Node::Unknown;
const STR_LIST: Node = Node::Array(&STR);
const UNKNOWN_LIST: Node = Node::Array(&UNKNOWN);
const UNKNOWN_RECORD: Node = Node::Record {
    key: &STR,
    value: &UNKNOWN,
};
/// `z.enum(["one", "two"])` (Wilson `sided`).
const SIDED: Node = Node::Enum(&["one", "two"]);
