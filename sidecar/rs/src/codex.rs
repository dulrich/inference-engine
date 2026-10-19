//! The typed codex app-server boundary: the ids, the usage figure and the frames the sidecar
//! consumes. Everything codex sends is read here, once; the rest of the crate sees types.
//!
//! Normalization (off-schema frames only; the app-server schema sends strings and integers):
//! - an id that is a number is stringified once, here (JS `String(n)` text); an empty string
//!   or any other type reads as absent;
//! - a usage count that is missing, null, negative or non-numeric reads as 0;
//! - a field of the wrong shape reads as absent rather than failing the whole frame, so an
//!   off-schema field can never drop a frame the security boundary needs to see.

use serde::de::{DeserializeOwned, Deserializer};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// A JSON number as JS `String(n)` spells it for the ids codex sends: integral values (within
/// 2^53) without a fraction.
fn number_text(n: &serde_json::Number) -> String {
    match n.as_f64() {
        Some(f) if f.fract() == 0.0 && f.abs() < 9_007_199_254_740_992.0 => (f as i64).to_string(),
        _ => n.to_string(),
    }
}

fn id_text(v: &Value) -> Option<String> {
    match v {
        Value::String(s) if !s.is_empty() => Some(s.clone()),
        Value::Number(n) => Some(number_text(n)),
        _ => None,
    }
}

macro_rules! codex_id {
    ($(#[$m:meta])* $name:ident) => {
        $(#[$m])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, Default, Serialize)]
        #[serde(transparent)]
        pub struct $name(pub String);

        impl $name {
            /// The id in `v`, normalized (see the module doc); `None` when absent.
            pub fn from_value(v: &Value) -> Option<Self> {
                id_text(v).map(Self)
            }

            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                let v = Value::deserialize(d)?;
                Self::from_value(&v).ok_or_else(|| serde::de::Error::custom("not a codex id"))
            }
        }

        impl From<&$name> for Value {
            fn from(i: &$name) -> Value {
                Value::String(i.0.clone())
            }
        }
    };
}

codex_id!(
    /// A codex thread id (the `bus` key).
    ThreadId
);
codex_id!(
    /// A codex turn id (`turn/interrupt`).
    TurnId
);
codex_id!(
    /// A dynamic-tool call id (`item/tool/call` `callId`; the client's `tool_use.id`).
    CallId
);
codex_id!(
    /// An item id (`item/agentMessage/delta` `itemId`, `item/completed` `item.id`).
    ItemId
);

/// `Option<Value>` that keeps a present `null` as `Some(Null)` (JS `!== undefined`).
fn present<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Value>, D::Error> {
    Value::deserialize(d).map(Some)
}

/// A field of the wrong shape reads as absent instead of failing the frame.
fn lenient<'de, D: Deserializer<'de>, T: DeserializeOwned>(d: D) -> Result<Option<T>, D::Error> {
    Ok(serde_json::from_value(Value::deserialize(d)?).ok())
}

/// A JSON-RPC frame from the app-server: a response (`id` + `result`/`error`), a server
/// request (`id` + `method`), or a notification (`method`). `params` stays raw until the
/// method is known ([`Note::parse`], [`ToolCall`]).
#[derive(Debug, Default, Deserialize)]
#[serde(default)]
pub struct RpcFrame {
    #[serde(deserialize_with = "present")]
    pub id: Option<Value>,
    #[serde(deserialize_with = "present")]
    pub method: Option<Value>,
    pub params: Option<Value>,
    pub result: Option<Value>,
    pub error: Option<Value>,
}

impl RpcFrame {
    pub fn method_str(&self) -> Option<&str> {
        self.method.as_ref().and_then(Value::as_str)
    }

    /// `params.threadId`, the bus routing key.
    pub fn thread_id(&self) -> Option<ThreadId> {
        self.params
            .as_ref()
            .and_then(|p| p.get("threadId"))
            .and_then(ThreadId::from_value)
    }
}

fn parse_params<T: DeserializeOwned + Default>(params: Option<&Value>) -> T {
    match params {
        Some(p @ Value::Object(_)) => serde_json::from_value(p.clone()).unwrap_or_default(),
        _ => T::default(),
    }
}

/// The four counts the sidecar reads from a codex usage figure.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Usage {
    pub input: u64,
    pub cached: u64,
    pub cache_write: u64,
    pub output: u64,
}

fn count(v: Option<&Value>) -> u64 {
    match v {
        Some(Value::Number(n)) => n
            .as_u64()
            .or_else(|| {
                n.as_f64()
                    .filter(|f| f.is_finite() && *f > 0.0)
                    .map(|f| f as u64)
            })
            .unwrap_or(0),
        _ => 0,
    }
}

impl Usage {
    /// The counts in a codex usage object; a missing/null/non-numeric field reads 0.
    pub fn from_value(v: &Value) -> Self {
        Self {
            input: count(v.get("inputTokens")),
            cached: count(v.get("cachedInputTokens")),
            cache_write: count(v.get("cacheWriteInputTokens")),
            output: count(v.get("outputTokens")),
        }
    }

    /// The counts in codex's names.
    pub fn to_value(&self) -> Value {
        json!({
            "inputTokens": self.input,
            "cachedInputTokens": self.cached,
            "cacheWriteInputTokens": self.cache_write,
            "outputTokens": self.output,
        })
    }

    /// A compaction interim: every input field 0.
    pub fn input_is_zero(&self) -> bool {
        self.input == 0 && self.cached == 0 && self.cache_write == 0
    }
}

/// A codex usage figure: the typed counts, plus the object exactly as codex sent it (every
/// field and its presence; logged as `usageTotalCodex`).
#[derive(Debug, Clone, PartialEq)]
pub struct CodexUsage {
    pub usage: Usage,
    pub raw: Value,
}

impl<'de> Deserialize<'de> for CodexUsage {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let raw = Value::deserialize(d)?;
        if !raw.is_object() {
            return Err(serde::de::Error::custom("usage is not an object"));
        }
        Ok(Self {
            usage: Usage::from_value(&raw),
            raw,
        })
    }
}

/// `thread/tokenUsage/updated` `tokenUsage` (also on `turn/completed`).
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct TokenUsage {
    #[serde(deserialize_with = "lenient")]
    pub last: Option<CodexUsage>,
    #[serde(deserialize_with = "lenient")]
    pub total: Option<CodexUsage>,
    #[serde(deserialize_with = "lenient")]
    pub model_context_window: Option<u64>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Turn {
    #[serde(deserialize_with = "lenient")]
    pub id: Option<TurnId>,
    #[serde(deserialize_with = "lenient")]
    pub status: Option<String>,
    pub error: Option<Value>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Item {
    /// Raw: any non-in-band type, whatever its JSON type, trips the boundary.
    #[serde(rename = "type")]
    pub ty: Option<Value>,
    #[serde(deserialize_with = "lenient")]
    pub id: Option<ItemId>,
    #[serde(deserialize_with = "lenient")]
    pub text: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct TurnParams {
    #[serde(deserialize_with = "lenient")]
    turn: Option<Turn>,
    #[serde(deserialize_with = "lenient")]
    token_usage: Option<TokenUsage>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct ItemParams {
    #[serde(deserialize_with = "lenient")]
    turn_id: Option<TurnId>,
    #[serde(deserialize_with = "lenient")]
    item: Option<Item>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct DeltaParams {
    #[serde(deserialize_with = "lenient")]
    item_id: Option<ItemId>,
    #[serde(deserialize_with = "lenient")]
    delta: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct UsageParams {
    #[serde(deserialize_with = "lenient")]
    token_usage: Option<TokenUsage>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct RateLimitParams {
    rate_limits: Option<Value>,
}

/// A thread-scoped notification the session consumes; unknown methods are `Other`.
#[derive(Debug, Clone)]
pub enum Note {
    TurnStarted {
        turn_id: Option<TurnId>,
    },
    ItemStarted {
        turn_id: Option<TurnId>,
        item_type: Option<Value>,
    },
    AgentMessageDelta {
        item_id: Option<ItemId>,
        delta: String,
    },
    ItemCompleted(Item),
    TokenUsage(TokenUsage),
    /// `hook/started` / `mcpServer/startupStatus/updated`: the thread config was not honoured.
    ConfigEvent(&'static str),
    /// `error`: `params.error`, else the whole params.
    Error(Value),
    TurnCompleted {
        turn: Turn,
        token_usage: Option<TokenUsage>,
    },
    Other,
}

pub const HOOK_STARTED: &str = "hook/started";
pub const MCP_STARTUP: &str = "mcpServer/startupStatus/updated";

impl Note {
    pub fn parse(method: &str, params: Option<&Value>) -> Note {
        match method {
            "turn/started" => {
                let p: TurnParams = parse_params(params);
                Note::TurnStarted {
                    turn_id: p.turn.and_then(|t| t.id),
                }
            }
            "item/started" => {
                let p: ItemParams = parse_params(params);
                Note::ItemStarted {
                    turn_id: p.turn_id,
                    item_type: p.item.and_then(|i| i.ty),
                }
            }
            "item/agentMessage/delta" => {
                let p: DeltaParams = parse_params(params);
                Note::AgentMessageDelta {
                    item_id: p.item_id,
                    delta: p.delta.unwrap_or_default(),
                }
            }
            "item/completed" => {
                let p: ItemParams = parse_params(params);
                Note::ItemCompleted(p.item.unwrap_or_default())
            }
            "thread/tokenUsage/updated" => {
                let p: UsageParams = parse_params(params);
                p.token_usage.map_or(Note::Other, Note::TokenUsage)
            }
            HOOK_STARTED => Note::ConfigEvent(HOOK_STARTED),
            MCP_STARTUP => Note::ConfigEvent(MCP_STARTUP),
            "error" => {
                let p = match params {
                    Some(p @ Value::Object(_)) => p.clone(),
                    _ => json!({}),
                };
                Note::Error(match p.get("error") {
                    Some(v) if !v.is_null() => v.clone(),
                    _ => p,
                })
            }
            "turn/completed" => {
                let p: TurnParams = parse_params(params);
                Note::TurnCompleted {
                    turn: p.turn.unwrap_or_default(),
                    token_usage: p.token_usage,
                }
            }
            _ => Note::Other,
        }
    }
}

/// `account/rateLimits/updated` `params.rateLimits` (read by `rate_limit_exhaustion`).
pub fn rate_limits(params: Option<&Value>) -> Option<Value> {
    parse_params::<RateLimitParams>(params).rate_limits
}

/// `item/tool/call` params.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct ToolCallParams {
    #[serde(deserialize_with = "lenient")]
    pub thread_id: Option<ThreadId>,
    #[serde(deserialize_with = "lenient")]
    pub turn_id: Option<TurnId>,
    #[serde(deserialize_with = "lenient")]
    pub call_id: Option<CallId>,
    #[serde(deserialize_with = "lenient")]
    pub tool: Option<String>,
    pub arguments: Option<Value>,
}

/// One `item/tool/call` server request: the JSON-RPC id to answer, and its params.
#[derive(Debug, Clone)]
pub struct ToolCall {
    pub rpc_id: Value,
    pub params: ToolCallParams,
}

impl ToolCall {
    pub fn of(frame: &RpcFrame) -> Self {
        Self {
            rpc_id: frame.id.clone().unwrap_or(Value::Null),
            params: parse_params(frame.params.as_ref()),
        }
    }

    /// The parked-call key; an absent call id parks under `""` so abandonment still answers it.
    pub fn call_id(&self) -> CallId {
        self.params.call_id.clone().unwrap_or_default()
    }
}

/// The thread id in a `thread/start` result (`thread.id`, else `threadId`).
pub fn started_thread_id(result: &Value) -> Option<ThreadId> {
    result
        .get("thread")
        .and_then(|t| t.get("id"))
        .and_then(ThreadId::from_value)
        .or_else(|| result.get("threadId").and_then(ThreadId::from_value))
}

/// The turn id in a `turn/start` result (`turn.id`, else `turnId`).
pub fn started_turn_id(result: &Value) -> Option<TurnId> {
    result
        .get("turn")
        .and_then(|t| t.get("id"))
        .and_then(TurnId::from_value)
        .or_else(|| result.get("turnId").and_then(TurnId::from_value))
}
