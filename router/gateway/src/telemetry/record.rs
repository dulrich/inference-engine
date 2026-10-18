//! The 0006 per-request context: `progress` lines at ≤1/s from request start (streaming
//! requests only, as in 0006), and exactly one terminal `request` line.
//!
//! Redaction is structural: only the allowlisted scalars below are ever copied into a record.
//! Headers are never read; from the client body only `model`, the `_session_` segment of
//! `metadata.user_id` and the `cc_is_subagent=true` witness boolean are derived; from the
//! upstream stream only model id, stop reason, usage counters and delta *counts*.

use super::sink::{iso8601, Sink};
use super::{Outcome, RequestStart, RequestTelemetry, Resolution, Telemetry};
use crate::relay::Route;
use serde_json::{json, Map, Value};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime};
use tokio::task::JoinHandle;
use tokio::time::Instant;

const PROGRESS_INTERVAL: Duration = Duration::from_millis(1000);
const MAX_PROGRESS_RECORDS: u64 = 3600;
/// A single SSE data line longer than this is not parsed (0006).
const MAX_PARSED_LINE: usize = 64 * 1024;
/// Carry-over cap for a partial SSE line (0006).
const MAX_RESIDUAL: usize = 256 * 1024;
/// Buffered (non-SSE) JSON bodies larger than this are not parsed.
const MAX_JSON_BODY: usize = 8 * 1024 * 1024;

pub struct JsonlTelemetry {
    sink: Arc<Sink>,
}

impl JsonlTelemetry {
    pub fn new(sink: Arc<Sink>) -> Self {
        JsonlTelemetry { sink }
    }
}

fn route_str(r: Route) -> (&'static str, &'static str) {
    match r {
        Route::Messages => ("/v1/messages", "anthropic_messages"),
        Route::CountTokens => ("/v1/messages/count_tokens", "anthropic_messages"),
        Route::ChatCompletions => ("/v1/chat/completions", "openai_chat_completions"),
        Route::Systemone => ("/v1/systemone", "systemone"),
    }
}

fn nonempty(v: Option<&Value>) -> Option<&str> {
    v.and_then(Value::as_str).filter(|s| !s.trim().is_empty())
}

/// The `_session_<id>` segment of `metadata.user_id` (≥8 of `[A-Za-z0-9-]`), else
/// `metadata.session_id`/`sessionId`. The account/user segments are never returned.
pub fn read_session_id(body: &Value) -> Option<String> {
    let meta = body.get("metadata").filter(|m| m.is_object())?;
    if let Some(uid) = nonempty(meta.get("user_id")) {
        let mut rest = uid;
        while let Some(at) = rest.find("_session_") {
            let tail = &rest[at + "_session_".len()..];
            let id: String = tail
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric() || *c == '-')
                .collect();
            if id.len() >= 8 {
                return Some(id);
            }
            rest = &rest[at + 1..];
        }
    }
    nonempty(meta.get("session_id"))
        .or_else(|| nonempty(meta.get("sessionId")))
        .map(str::to_string)
}

/// `cc_is_subagent=true` in the string `system` or the first `system` block's text.
pub fn read_subagent_witness(body: &Value) -> bool {
    let text = match body.get("system") {
        Some(Value::String(s)) => Some(s.as_str()),
        Some(Value::Array(a)) => a
            .first()
            .and_then(|b| b.get("text"))
            .and_then(Value::as_str),
        _ => None,
    };
    text.is_some_and(|t| t.contains("cc_is_subagent=true"))
}

#[derive(Default)]
struct Usage {
    input: Option<Value>,
    output: Option<Value>,
    cache_read: Option<Value>,
    cache_write: Option<Value>,
}

fn num(v: Option<&Value>) -> Option<Value> {
    v.filter(|n| n.is_number()).cloned()
}

impl Usage {
    fn merge(&mut self, o: Usage) {
        self.input = o.input.or(self.input.take());
        self.output = o.output.or(self.output.take());
        self.cache_read = o.cache_read.or(self.cache_read.take());
        self.cache_write = o.cache_write.or(self.cache_write.take());
    }
    fn anthropic(u: Option<&Value>) -> Usage {
        let Some(u) = u.filter(|u| u.is_object()) else {
            return Usage::default();
        };
        Usage {
            input: num(u.get("input_tokens")),
            output: num(u.get("output_tokens")),
            cache_read: num(u.get("cache_read_input_tokens")),
            cache_write: num(u.get("cache_creation_input_tokens")),
        }
    }
    fn openai(u: Option<&Value>) -> Usage {
        let Some(u) = u.filter(|u| u.is_object()) else {
            return Usage::default();
        };
        Usage {
            input: num(u.get("prompt_tokens")),
            output: num(u.get("completion_tokens")),
            cache_read: num(u.pointer("/prompt_tokens_details/cached_tokens")),
            cache_write: None,
        }
    }
    fn to_json(&self) -> Value {
        json!({"input": self.input, "output": self.output,
               "cacheRead": self.cache_read, "cacheWrite": self.cache_write})
    }
}

#[derive(PartialEq)]
enum BodyMode {
    Unknown,
    Sse,
    Json,
}

struct State {
    started: Instant,
    req_id: String,
    route: &'static str,
    source_adapter_key: &'static str,
    model_requested: Option<String>,
    session_id: Option<String>,
    is_subagent: bool,
    stream: bool,
    leg: Option<String>,
    provider_type: Option<String>,
    upstream_url: Option<String>,
    attempt_sequence: u64,
    model_resolved: Option<String>,
    stop_reason: Option<String>,
    usage: Usage,
    first_byte: Option<Instant>,
    first_content: Option<Instant>,
    response_tokens: u64,
    reasoning_tokens: u64,
    progress_count: u64,
    finalized: bool,
    mode: BodyMode,
    residual: Vec<u8>,
}

fn ms(d: Duration) -> u64 {
    d.as_millis() as u64
}

impl State {
    fn progress(&mut self) -> Option<Value> {
        if self.finalized || self.progress_count >= MAX_PROGRESS_RECORDS {
            return None;
        }
        self.progress_count += 1;
        Some(json!({
            "kind": "progress",
            "ts": iso8601(SystemTime::now()),
            "reqId": self.req_id,
            "sessionId": self.session_id,
            "leg": self.leg,
            "modelResolved": self.model_resolved,
            "phase": if self.first_content.is_none() { "prefill" } else { "decode" },
            "responseTokens": self.response_tokens,
            "reasoningTokens": self.reasoning_tokens,
            "elapsedMs": ms(self.started.elapsed()),
        }))
    }

    fn mark_content(&mut self) {
        self.first_content.get_or_insert_with(Instant::now);
    }

    fn chunk(&mut self, chunk: &[u8]) {
        if chunk.is_empty() {
            return;
        }
        self.first_byte.get_or_insert_with(Instant::now);
        if self.mode == BodyMode::Unknown {
            match chunk.iter().find(|b| !b.is_ascii_whitespace()) {
                Some(b'{') | Some(b'[') => self.mode = BodyMode::Json,
                Some(_) => self.mode = BodyMode::Sse,
                None => return,
            }
        }
        if self.mode == BodyMode::Json {
            if self.residual.len() + chunk.len() <= MAX_JSON_BODY {
                self.residual.extend_from_slice(chunk);
            } else {
                self.residual = Vec::new();
            }
            return;
        }
        self.residual.extend_from_slice(chunk);
        let Some(last_nl) = self.residual.iter().rposition(|b| *b == b'\n') else {
            if self.residual.len() > MAX_RESIDUAL {
                self.residual.clear();
            }
            return;
        };
        let tail = self.residual.split_off(last_nl + 1);
        let complete = std::mem::replace(&mut self.residual, tail);
        for line in complete.split(|b| *b == b'\n') {
            let Some(data) = std::str::from_utf8(line)
                .ok()
                .map(str::trim)
                .and_then(|l| l.strip_prefix("data:"))
                .map(str::trim)
            else {
                continue;
            };
            if data.is_empty() || data == "[DONE]" || data.len() > MAX_PARSED_LINE {
                continue;
            }
            if let Ok(Value::Object(p)) = serde_json::from_str::<Value>(data) {
                self.event(&p);
            }
        }
        if self.residual.len() > MAX_RESIDUAL {
            self.residual.clear();
        }
    }

    /// One SSE payload: Anthropic event when it carries `type`, else an OpenAI chunk.
    fn event(&mut self, p: &Map<String, Value>) {
        match p.get("type").and_then(Value::as_str) {
            Some("message_start") => {
                if let Some(m) = nonempty(p.get("message").and_then(|m| m.get("model"))) {
                    self.model_resolved = Some(m.to_string());
                }
                self.usage.merge(Usage::anthropic(
                    p.get("message").and_then(|m| m.get("usage")),
                ));
            }
            Some("content_block_delta") => {
                let thinking = p
                    .get("delta")
                    .and_then(|d| d.get("type"))
                    .and_then(Value::as_str)
                    == Some("thinking_delta");
                if thinking {
                    self.reasoning_tokens += 1;
                } else {
                    self.response_tokens += 1;
                }
                self.mark_content();
            }
            Some("message_delta") => {
                if let Some(s) = nonempty(p.get("delta").and_then(|d| d.get("stop_reason"))) {
                    self.stop_reason = Some(s.to_string());
                }
                self.usage.merge(Usage::anthropic(p.get("usage")));
            }
            Some(_) => {}
            None => self.openai_chunk(p),
        }
    }

    fn openai_chunk(&mut self, p: &Map<String, Value>) {
        if self.model_resolved.is_none() {
            self.model_resolved = nonempty(p.get("model")).map(str::to_string);
        }
        self.usage.merge(Usage::openai(p.get("usage")));
        for choice in p
            .get("choices")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if let Some(f) = nonempty(choice.get("finish_reason")) {
                self.stop_reason = Some(f.to_string());
            }
            let Some(delta) = choice.get("delta") else {
                continue;
            };
            if nonempty(delta.get("reasoning_content"))
                .or_else(|| nonempty(delta.get("reasoning")))
                .is_some()
            {
                self.reasoning_tokens += 1;
                self.mark_content();
            }
            if nonempty(delta.get("content")).is_some() {
                self.response_tokens += 1;
                self.mark_content();
            }
        }
    }

    /// A buffered (non-streamed) JSON response: model, stop reason and usage only.
    fn json_body(&mut self) {
        let Ok(Value::Object(p)) = serde_json::from_slice::<Value>(&self.residual) else {
            return;
        };
        if p.get("type").and_then(Value::as_str) == Some("message") {
            if let Some(m) = nonempty(p.get("model")) {
                self.model_resolved = Some(m.to_string());
            }
            if let Some(s) = nonempty(p.get("stop_reason")) {
                self.stop_reason = Some(s.to_string());
            }
            self.usage.merge(Usage::anthropic(p.get("usage")));
        } else if p.contains_key("choices") {
            if self.model_resolved.is_none() {
                self.model_resolved = nonempty(p.get("model")).map(str::to_string);
            }
            if let Some(f) = nonempty(p.get("choices").and_then(|c| c.pointer("/0/finish_reason")))
            {
                self.stop_reason = Some(f.to_string());
            }
            self.usage.merge(Usage::openai(p.get("usage")));
        }
    }

    fn terminal(&mut self, o: &Outcome) -> Value {
        self.finalized = true;
        if self.mode == BodyMode::Json {
            self.json_body();
        }
        let since = |t: Option<Instant>| t.map(|t| ms(t.saturating_duration_since(self.started)));
        json!({
            "kind": "request",
            "ts": iso8601(SystemTime::now()),
            "reqId": self.req_id,
            "route": self.route,
            "method": "POST",
            "sourceAdapterKey": self.source_adapter_key,
            "leg": self.leg,
            "providerType": self.provider_type,
            "upstreamUrl": self.upstream_url,
            "sessionId": self.session_id,
            "isSubagent": self.is_subagent,
            "modelRequested": self.model_requested,
            "modelResolved": self.model_resolved,
            "stream": self.stream,
            "status": o.status,
            "attemptSequence": self.attempt_sequence,
            "usage": self.usage.to_json(),
            "responseTokens": self.response_tokens,
            "reasoningTokens": self.reasoning_tokens,
            "ttftMs": since(self.first_content),
            "upstreamFirstByteMs": since(self.first_byte),
            "wallMs": ms(o.elapsed),
            "upstreamTimings": Value::Null,
            "stopReason": self.stop_reason,
            "error": o.error.as_ref().map(|(stage, message)| json!({"stage": stage, "message": message})),
        })
    }
}

struct Shared {
    sink: Arc<Sink>,
    state: Mutex<State>,
}

struct JsonlRequest {
    shared: Arc<Shared>,
    ticker: Option<JoinHandle<()>>,
}

impl Telemetry for JsonlTelemetry {
    fn request_start(&self, s: &RequestStart<'_>) -> Box<dyn RequestTelemetry> {
        let (route, key) = route_str(s.route);
        let body = s.body.unwrap_or(&Value::Null);
        let state = State {
            started: Instant::now(),
            req_id: s.request_id.to_string(),
            route,
            source_adapter_key: key,
            model_requested: s
                .requested_model
                .filter(|m| !m.trim().is_empty())
                .map(str::to_string),
            session_id: read_session_id(body),
            is_subagent: read_subagent_witness(body),
            stream: s.streaming,
            leg: None,
            provider_type: None,
            upstream_url: None,
            attempt_sequence: 0,
            model_resolved: None,
            stop_reason: None,
            usage: Usage::default(),
            first_byte: None,
            first_content: None,
            response_tokens: 0,
            reasoning_tokens: 0,
            progress_count: 0,
            finalized: false,
            mode: BodyMode::Unknown,
            residual: Vec::new(),
        };
        let shared = Arc::new(Shared {
            sink: self.sink.clone(),
            state: Mutex::new(state),
        });
        // Progress cadence from request start (before upstream headers), streaming only.
        let ticker = (s.streaming && !self.sink.is_disabled())
            .then(tokio::runtime::Handle::try_current)
            .and_then(Result::ok)
            .map(|rt| {
                let weak = Arc::downgrade(&shared);
                rt.spawn(async move {
                    let mut iv = tokio::time::interval_at(
                        Instant::now() + PROGRESS_INTERVAL,
                        PROGRESS_INTERVAL,
                    );
                    loop {
                        iv.tick().await;
                        let Some(sh) = weak.upgrade() else { break };
                        // Written under the lock so no progress line can follow the terminal.
                        let Ok(mut st) = sh.state.lock() else { break };
                        match st.progress() {
                            Some(r) => sh.sink.write(&r),
                            None => break,
                        }
                    }
                })
            });
        Box::new(JsonlRequest { shared, ticker })
    }
}

impl RequestTelemetry for JsonlRequest {
    fn resolved(&mut self, r: &Resolution<'_>) {
        if let Ok(mut st) = self.shared.state.lock() {
            st.attempt_sequence += 1;
            st.leg = Some(r.provider_name.to_string());
            st.provider_type = Some(r.provider_kind.to_string());
            st.upstream_url = Some(r.upstream_url.to_string());
            if !r.model.is_empty() {
                st.model_resolved = Some(r.model.to_string());
            }
        }
    }

    fn upstream_chunk(&mut self, chunk: &[u8]) {
        if let Ok(mut st) = self.shared.state.lock() {
            st.chunk(chunk);
        }
    }

    fn finalize(mut self: Box<Self>, outcome: &Outcome) {
        if let Some(t) = self.ticker.take() {
            t.abort();
        }
        if let Ok(mut st) = self.shared.state.lock() {
            if !st.finalized {
                let r = st.terminal(outcome);
                self.shared.sink.write(&r);
            }
        }
    }
}

impl Drop for JsonlRequest {
    fn drop(&mut self) {
        if let Some(t) = self.ticker.take() {
            t.abort();
        }
    }
}
