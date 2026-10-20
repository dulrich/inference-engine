//! Black-box tests: the gateway on 127.0.0.1:0 against an in-process recording stub upstream.
//! Fake credentials only; no egress.

use axum::body::{Body, Bytes};
use axum::http::{HeaderMap, StatusCode, Uri};
use axum::response::Response;
use inference_router::{app, catalog, config, healthcheck, telemetry};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[derive(Clone, Debug)]
struct Seen {
    path: String,
    headers: HeaderMap,
    body: Value,
    raw: Bytes,
}

type Script = HashMap<&'static str, (u16, &'static str, &'static str)>;

#[derive(Clone)]
struct Stub {
    seen: Arc<Mutex<Vec<Seen>>>,
    addr: SocketAddr,
}

async fn stub(script: Script) -> Stub {
    let seen: Arc<Mutex<Vec<Seen>>> = Arc::default();
    let (s2, script) = (seen.clone(), Arc::new(script));
    let handler = move |uri: Uri, headers: HeaderMap, body: Bytes| {
        let (seen, script) = (s2.clone(), script.clone());
        async move {
            let path = uri
                .path_and_query()
                .map(|p| p.to_string())
                .unwrap_or_default();
            seen.lock().unwrap().push(Seen {
                path: path.clone(),
                headers,
                body: serde_json::from_slice(&body).unwrap_or(Value::Null),
                raw: body,
            });
            let (status, ct, text) = script.get(uri.path()).copied().unwrap_or((
                200,
                "application/json",
                "{\"ok\":true}",
            ));
            if text == "SLOW" {
                tokio::time::sleep(Duration::from_secs(10)).await;
            }
            Response::builder()
                .status(status)
                .header("content-type", ct)
                .body(Body::from(text))
                .unwrap()
        }
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, axum::Router::new().fallback(handler))
            .await
            .unwrap()
    });
    Stub { seen, addr }
}

impl Stub {
    fn seen(&self) -> Vec<Seen> {
        self.seen.lock().unwrap().clone()
    }
}

const KEY: &str = "test-gateway-key";

async fn gateway(up: &Stub, extra_env: &[(&str, &str)]) -> String {
    let base = format!("http://{}", up.addr);
    let raw = json!({"host": "127.0.0.1", "port": 0, "providers": [
        {"name": "claude-local", "type": "openai_chat_completions", "localLeg": "native-messages",
         "apikey": "local-dummy", "baseurl": format!("{base}/local/v1"), "models": ["qwen", "shared"],
         "effortKwargs": {"qwen": {"low": {"reasoning_effort": "low"}}}, "maxOutputTokens": {"qwen": 32000}},
        {"name": "anthropic", "type": "anthropic", "credential": "caller-oauth-passthrough", "baseurl": format!("{base}/anthropic"), "models": ["claude-x", "shared"]},
        {"name": "openrouter", "type": "openai_chat_completions", "apiKeyEnv": "OR_KEY", "baseurl": format!("{base}/or/v1"), "models": ["qwen/q"]},
        {"name": "or-anthropic", "type": "anthropic", "apiKeyEnv": "OR_KEY", "baseurl": format!("{base}/or"), "models": ["qwen/q"]},
        {"name": "codex-a", "type": "anthropic", "apiKeyEnv": "SIDE_KEY", "baseurl": format!("{base}/codex-a"), "models": ["gpt-x"]},
        {"name": "nocred", "type": "anthropic", "apiKeyEnv": "UNSET_KEY", "baseurl": format!("{base}/nocred"), "models": ["n"]},
        {"name": "openrouter-decide", "type": "systemone", "apiKeyEnv": "OR_KEY", "baseurl": format!("{base}/decide/v1"), "models": ["typesafe/jev"]},
        {"name": "decide-nocred", "type": "systemone", "baseurl": format!("{base}/x/v1"), "models": ["m"]}
    ]});
    let env: HashMap<String, String> = [
        ("AUTH_STATIC_API_KEYS", KEY),
        ("OR_KEY", "or-secret"),
        ("SIDE_KEY", "side-secret"),
        ("ANTHROPIC_API_KEY", "must-never-be-read"),
    ]
    .iter()
    .chain(extra_env)
    .map(|(k, v)| (k.to_string(), v.to_string()))
    .collect();
    let get = move |k: &str| env.get(k).cloned();
    let settings = config::from_json(&raw, &get).unwrap();
    let state = legacy_state(settings, &get);
    let (tx, rx) = tokio::sync::oneshot::channel();
    tokio::spawn(app::serve(state, move |a| tx.send(a).unwrap()));
    format!("http://{}", rx.await.unwrap())
}

async fn post(
    gw: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: Value,
) -> (u16, HeaderMap, String) {
    let mut req = reqwest::Client::new()
        .post(format!("{gw}{path}"))
        .header("x-gateway-key", KEY)
        .body(body.to_string());
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    let r = req.send().await.unwrap();
    let (s, h) = (r.status().as_u16(), r.headers().clone());
    (s, h, r.text().await.unwrap())
}

/// App state whose catalog is derived from the fixture config's legacy per-model keys.
fn legacy_state(settings: config::Settings, env: config::Env) -> Arc<app::AppState> {
    let holder = catalog::legacy_holder(&settings).unwrap();
    app::state_with(settings, holder, telemetry::from_env(env)).unwrap()
}

fn keys_env(k: &str) -> Option<String> {
    (k == "AUTH_STATIC_API_KEYS").then(|| KEY.to_string())
}

fn h<'a>(s: &'a Seen, k: &str) -> Option<&'a str> {
    s.headers.get(k).map(|v| v.to_str().unwrap())
}

#[tokio::test]
async fn anthropic_leg_oauth_passthrough_and_header_forwarding() {
    let up = stub(Script::new()).await;
    let gw = gateway(&up, &[]).await;
    let hdrs = [
        ("authorization", "Bearer oauth-tok"),
        ("anthropic-version", "2023-06-01"),
        ("anthropic-beta", "b1"),
        ("user-agent", "cc"),
    ];
    let (s, rh, _) = post(
        &gw,
        "/v1/messages?beta=true",
        &hdrs,
        json!({"model": "claude-x", "max_tokens": 5, "messages": []}),
    )
    .await;
    assert_eq!(s, 200);
    assert_eq!(
        rh.get("x-gateway-target-provider-name").unwrap(),
        "anthropic"
    );
    let seen = up.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/anthropic/v1/messages");
    assert_eq!(h(&seen[0], "authorization"), Some("Bearer oauth-tok"));
    assert_eq!(h(&seen[0], "x-api-key"), None);
    assert_eq!(h(&seen[0], "x-gateway-key"), None);
    assert_eq!(
        (
            h(&seen[0], "anthropic-version"),
            h(&seen[0], "anthropic-beta")
        ),
        (Some("2023-06-01"), Some("b1"))
    );
    assert_eq!(
        seen[0].body,
        json!({"model": "claude-x", "max_tokens": 5, "messages": []})
    );
    // Pin 9: no caller credential on the passthrough leg is a clear 401, no upstream call.
    let (s, _, b) = post(
        &gw,
        "/v1/messages",
        &[],
        json!({"model": "anthropic/claude-x"}),
    )
    .await;
    assert_eq!(
        (s, b.as_str()),
        (
            401,
            r#"{"error":{"message":"No caller credential for the anthropic provider."}}"#
        )
    );
    assert_eq!(up.seen().len(), 1);
}

#[tokio::test]
async fn credentialed_legs_replace_bearer_and_uncredentialed_is_refused() {
    let up = stub(Script::new()).await;
    let gw = gateway(&up, &[]).await;
    let oauth = [("authorization", "Bearer oauth-tok")];
    post(
        &gw,
        "/v1/messages",
        &oauth,
        json!({"model": "codex-a/gpt-x"}),
    )
    .await;
    post(
        &gw,
        "/v1/messages",
        &oauth,
        json!({"model": "claude-local/qwen"}),
    )
    .await;
    post(
        &gw,
        "/v1/chat/completions",
        &oauth,
        json!({"model": "openrouter/qwen/q"}),
    )
    .await;
    let seen = up.seen();
    assert_eq!(seen[0].path, "/codex-a/v1/messages");
    assert_eq!(
        (h(&seen[0], "authorization"), h(&seen[0], "x-api-key")),
        (Some("Bearer side-secret"), Some("side-secret"))
    );
    assert_eq!(seen[0].body["model"], "gpt-x");
    assert_eq!(seen[1].path, "/local/v1/messages");
    assert_eq!(
        (h(&seen[1], "authorization"), h(&seen[1], "x-api-key")),
        (Some("Bearer local-dummy"), None)
    );
    assert_eq!(seen[2].path, "/or/v1/chat/completions");
    assert_eq!(h(&seen[2], "authorization"), Some("Bearer or-secret"));
    assert_eq!(seen[2].body["model"], "qwen/q");
    let (s, _, b) = post(&gw, "/v1/messages", &oauth, json!({"model": "nocred/n"})).await;
    assert_eq!(s, 503, "{b}");
    assert_eq!(
        up.seen().len(),
        3,
        "uncredentialed provider must never be called"
    );
}

#[tokio::test]
async fn protocol_mismatch_is_refused_without_upstream_call() {
    let up = stub(Script::new()).await;
    let gw = gateway(&up, &[]).await;
    for (path, model) in [
        ("/v1/messages", "openrouter/qwen/q"),
        ("/v1/chat/completions", "or-anthropic/qwen/q"),
        ("/v1/chat/completions", "claude-x"),
        ("/v1/messages", "openrouter-decide/typesafe/jev"),
        ("/v1/messages/count_tokens", "openrouter/qwen/q"),
    ] {
        let (s, _, b) = post(&gw, path, &[], json!({"model": model})).await;
        assert_eq!(s, 400, "{path} {model}");
        assert!(b.contains("protocol_mismatch"), "{b}");
    }
    assert!(up.seen().is_empty());
}

#[tokio::test]
async fn fallthrough_then_final_error_relay() {
    let mut script = Script::new();
    script.insert("/local/v1/messages", (500, "text/plain", "local boom"));
    let up = stub(script).await;
    let gw = gateway(&up, &[]).await;
    let oauth = [("authorization", "Bearer t")];
    // Bare "shared" → [claude-local, anthropic] (first provider per kind, config order). This
    // two-target resolution is unreachable from the production config (no bare id is listed by
    // two default-route providers there); this test is what keeps the non-final-attempt path
    // (next target on 5xx, then the 0003 final-attempt relay) covered.
    let (s, _, b) = post(&gw, "/v1/messages", &oauth, json!({"model": "shared"})).await;
    assert_eq!((s, b.as_str()), (200, "{\"ok\":true}"));
    let paths: Vec<_> = up.seen().into_iter().map(|s| s.path).collect();
    assert_eq!(paths, ["/local/v1/messages", "/anthropic/v1/messages"]);

    let mut script = Script::new();
    script.insert("/local/v1/messages", (500, "text/plain", "local boom"));
    script.insert(
        "/anthropic/v1/messages",
        (
            529,
            "application/json",
            "{\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\"}}",
        ),
    );
    script.insert("/local/v1/chat/completions", (502, "text/plain", "bad gw"));
    let up = stub(script).await;
    let gw = gateway(&up, &[]).await;
    let (s, rh, b) = post(&gw, "/v1/messages", &oauth, json!({"model": "shared"})).await;
    assert_eq!(
        (s, b.as_str()),
        (
            529,
            "{\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\"}}"
        )
    );
    assert_eq!(rh.get("content-type").unwrap(), "application/json");
    // Single-target final error on Messages ingress: verbatim even for non-JSON.
    let (s, _, b) = post(
        &gw,
        "/v1/messages",
        &[],
        json!({"model": "claude-local/qwen"}),
    )
    .await;
    assert_eq!((s, b.as_str()), (500, "local boom"));
    // Chat ingress keeps the fallthrough envelope (0003 is Anthropic-source only).
    let (s, _, b) = post(
        &gw,
        "/v1/chat/completions",
        &[],
        json!({"model": "claude-local/qwen"}),
    )
    .await;
    let v: Value = serde_json::from_str(&b).unwrap();
    assert_eq!(
        (s, v["error"]["message"].as_str()),
        (502, Some("All target providers failed."))
    );
    assert_eq!(
        v["error"]["attempts"][0]["details"],
        json!({"raw": "bad gw"})
    );
}

#[tokio::test]
async fn count_tokens_url_rule_and_sse_verbatim() {
    let sse =
        "event: message_start\ndata: {\"type\":\"message_start\"}\n\nevent: ping\ndata: {}\n\n";
    let mut script = Script::new();
    script.insert("/anthropic/v1/messages", (200, "text/event-stream", sse));
    script.insert("/or/v1/messages/count_tokens", (404, "text/plain", "nf"));
    let up = stub(script).await;
    let gw = gateway(&up, &[]).await;
    let oauth = [("authorization", "Bearer t")];
    let (s, rh, b) = post(
        &gw,
        "/v1/messages",
        &oauth,
        json!({"model": "claude-x", "stream": true}),
    )
    .await;
    assert_eq!((s, b.as_str()), (200, sse));
    assert_eq!(rh.get("content-type").unwrap(), "text/event-stream");
    post(
        &gw,
        "/v1/messages/count_tokens",
        &[],
        json!({"model": "claude-local/qwen"}),
    )
    .await;
    post(
        &gw,
        "/v1/messages/count_tokens",
        &oauth,
        json!({"model": "claude-x"}),
    )
    .await;
    let (s, _, b) = post(
        &gw,
        "/v1/messages/count_tokens",
        &[],
        json!({"model": "or-anthropic/qwen/q"}),
    )
    .await;
    assert_eq!((s, b.as_str()), (404, "nf"));
    let paths: Vec<_> = up.seen().into_iter().map(|s| s.path).collect();
    assert_eq!(
        paths[1..],
        [
            "/local/v1/messages/count_tokens",
            "/anthropic/v1/messages/count_tokens",
            "/or/v1/messages/count_tokens"
        ]
    );
    let (s, _, b) = post(
        &gw,
        "/v1/messages/count_tokens",
        &[],
        json!({"model": "nope"}),
    )
    .await;
    assert_eq!(s, 404);
    assert!(b.contains("model_not_found"));
}

#[tokio::test]
async fn systemone_allowlist_and_credentials() {
    let up = stub(Script::new()).await;
    let gw = gateway(&up, &[]).await;
    let oauth = [
        ("authorization", "Bearer oauth-tok"),
        ("x-api-key", "caller"),
    ];
    let body =
        json!({"model": "openrouter-decide/typesafe/jev", "state": "s", "questions": {"q": 1}});
    let (s, _, _) = post(&gw, "/v1/systemone", &oauth, body).await;
    assert_eq!(s, 200);
    for m in [
        "openrouter-decide/other",
        "nope/typesafe/jev",
        "anthropic/claude-x",
        "openrouter-decide",
        "typesafe/jev",
    ] {
        let (s, _, b) = post(&gw, "/v1/systemone", &oauth, json!({"model": m})).await;
        assert_eq!(s, 404, "{m}");
        assert!(b.contains("model_not_found"));
    }
    let (s, _, _) = post(
        &gw,
        "/v1/systemone",
        &oauth,
        json!({"model": "decide-nocred/m"}),
    )
    .await;
    assert_eq!(s, 503);
    let seen = up.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/decide/v1/systemone");
    assert_eq!(
        seen[0].body,
        json!({"model": "typesafe/jev", "state": "s", "questions": {"q": 1}})
    );
    assert_eq!(
        (h(&seen[0], "authorization"), h(&seen[0], "x-api-key")),
        (Some("Bearer or-secret"), None)
    );
}

#[tokio::test]
async fn auth_models_health_and_body_limit() {
    let up = stub(Script::new()).await;
    let gw = gateway(&up, &[("GATEWAY_BODY_LIMIT_BYTES", "64")]).await;
    let c = reqwest::Client::new();
    let r = c
        .post(format!("{gw}/v1/messages"))
        .body("{}")
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(
        r.text().await.unwrap(),
        "{\"error\":{\"message\":\"Missing auth token header: x-gateway-key\"}}"
    );
    let r = c.get(format!("{gw}/health")).send().await.unwrap();
    assert!(r.text().await.unwrap().starts_with("{\"status\":\"ok\""));
    let r = c
        .get(format!("{gw}/v1/models"))
        .header("x-gateway-key", KEY)
        .send()
        .await
        .unwrap();
    let v: Value = serde_json::from_str(&r.text().await.unwrap()).unwrap();
    assert_eq!(v["object"], "list");
    assert_eq!(
        v["data"][0],
        json!({"id": "claude-local/qwen", "object": "model", "created": 0, "owned_by": "claude-local"})
    );
    let r = c
        .get(format!("{gw}/v1/models"))
        .header("x-gateway-key", KEY)
        .header("anthropic-version", "2023-06-01")
        .send()
        .await
        .unwrap();
    let v: Value = serde_json::from_str(&r.text().await.unwrap()).unwrap();
    assert_eq!(
        (v["first_id"].as_str(), v["has_more"].as_bool()),
        (Some("claude-local/qwen"), Some(false))
    );
    let (s, _, _) = post(
        &gw,
        "/v1/messages",
        &[],
        json!({"model": "claude-x", "pad": "x".repeat(100)}),
    )
    .await;
    assert_eq!(s, 413);
    assert!(up.seen().is_empty());
}

#[tokio::test]
async fn healthcheck_contract() {
    let mut script = Script::new();
    script.insert(
        "/ok/healthz",
        (200, "application/json", "{\"status\":\"ok\"}"),
    );
    script.insert(
        "/degraded/healthz",
        (200, "application/json", "{\"status\":\"degraded\"}"),
    );
    script.insert("/slow/healthz", (200, "application/json", "SLOW"));
    let side = stub(script).await;
    let up = stub(Script::new()).await;
    let gw = gateway(&up, &[]).await;
    let own = format!("{gw}/health");
    let sc = |p: &str| format!("http://{}/{p}/healthz", side.addr);
    let t = Duration::from_millis(500);
    assert_eq!(
        healthcheck::check(&own, &sc("ok"), Some("sk"), t).await,
        Ok(())
    );
    assert_eq!(h(&side.seen()[0], "authorization"), Some("Bearer sk"));
    assert!(healthcheck::check(&own, &sc("degraded"), Some("sk"), t)
        .await
        .unwrap_err()
        .contains("degraded"));
    assert!(healthcheck::check(&own, &sc("slow"), Some("sk"), t)
        .await
        .is_err());
    // Own listener down → fail even when the sidecar is ok.
    assert!(
        healthcheck::check("http://127.0.0.1:9/health", &sc("ok"), Some("sk"), t)
            .await
            .is_err()
    );
    assert_eq!(
        (
            healthcheck::SIDECAR_URL_ENV,
            healthcheck::DEFAULT_SIDECAR_URL
        ),
        (
            "INFERENCE_ROUTER_HEALTHCHECK_SIDECAR_URL",
            "http://host.docker.internal:4877/healthz"
        )
    );
}

struct Recorder(Arc<Mutex<Vec<String>>>);
struct RecReq(Arc<Mutex<Vec<String>>>);

impl telemetry::RequestTelemetry for RecReq {
    fn resolved(&mut self, r: &telemetry::Resolution<'_>) {
        self.0
            .lock()
            .unwrap()
            .push(format!("resolved {}", r.provider_name));
    }
    fn upstream_chunk(&mut self, _chunk: &[u8]) {}
    fn finalize(self: Box<Self>, o: &telemetry::Outcome) {
        self.0
            .lock()
            .unwrap()
            .push(format!("finalize {:?} {}", o.kind, o.status));
    }
}

impl telemetry::Telemetry for Recorder {
    fn request_start(
        &self,
        s: &telemetry::RequestStart<'_>,
    ) -> Box<dyn telemetry::RequestTelemetry> {
        self.0.lock().unwrap().push(format!("start {:?}", s.route));
        Box::new(RecReq(self.0.clone()))
    }
}

#[tokio::test]
async fn telemetry_finalizes_once_on_success_and_client_disconnect() {
    let mut script = Script::new();
    script.insert(
        "/codex-a/v1/messages",
        (200, "application/json", "SLOW"),
    );
    let up = stub(script).await;
    let raw = json!({"host": "127.0.0.1", "port": 0, "providers": [
        {"name": "codex-a", "type": "anthropic", "apikey": "k", "baseurl": format!("http://{}/codex-a", up.addr), "models": ["gpt-x"]},
        {"name": "anthropic", "type": "anthropic", "credential": "caller-oauth-passthrough", "baseurl": format!("http://{}/anthropic", up.addr), "models": ["claude-x"]}]});
    let log: Arc<Mutex<Vec<String>>> = Arc::default();
    let settings = config::from_json(&raw, &keys_env).unwrap();
    let holder = catalog::legacy_holder(&settings).unwrap();
    let state = app::state_with(settings, holder, Arc::new(Recorder(log.clone()))).unwrap();
    let (tx, rx) = tokio::sync::oneshot::channel();
    tokio::spawn(app::serve(state, move |a| tx.send(a).unwrap()));
    let gw = format!("http://{}", rx.await.unwrap());
    let c = reqwest::Client::new();
    let ok = c
        .post(format!("{gw}/v1/messages"))
        .header("x-gateway-key", KEY)
        .header("authorization", "Bearer t")
        .body("{\"model\":\"anthropic/claude-x\"}")
        .send()
        .await
        .unwrap();
    assert_eq!(ok.text().await.unwrap(), "{\"ok\":true}");
    let dropped = c
        .post(format!("{gw}/v1/messages"))
        .header("x-gateway-key", KEY)
        .timeout(Duration::from_millis(300))
        .body("{\"model\":\"codex-a/gpt-x\"}")
        .send()
        .await;
    assert!(dropped.is_err());
    tokio::time::sleep(Duration::from_millis(300)).await;
    let log = log.lock().unwrap().clone();
    assert_eq!(
        log,
        [
            "start Messages",
            "resolved anthropic",
            "finalize Completed 200",
            "start Messages",
            "resolved codex-a",
            "finalize ClientDisconnect 499",
        ]
    );
}

#[tokio::test]
async fn removed_mimic_surfaces_are_ignored() {
    let up = stub(Script::new()).await;
    let gw = gateway(&up, &[]).await;
    // x-target-providers / x-target-provider are not read: "shared" routes as if absent.
    let hdrs = [
        ("authorization", "Bearer t"),
        ("x-target-providers", "anthropic"),
        ("x-target-provider", "nope"),
    ];
    let (s, rh, _) = post(&gw, "/v1/messages", &hdrs, json!({"model": "shared"})).await;
    assert_eq!(s, 200);
    assert_eq!(
        rh.get("x-gateway-target-provider-name").unwrap(),
        "claude-local"
    );
    // A type-route prefix is no longer a route: "anthropic" is matched only as a provider name.
    let (s, _, _) = post(
        &gw,
        "/v1/messages",
        &[
            ("authorization", "Bearer t"),
            ("x-anthropic-version", "2099-01-01"),
        ],
        json!({"model": "anthropic/claude-x"}),
    )
    .await;
    assert_eq!(s, 200);
    let seen = up.seen();
    let paths: Vec<_> = seen.iter().map(|s| s.path.as_str()).collect();
    assert_eq!(paths, ["/local/v1/messages", "/anthropic/v1/messages"]);
    // The x-anthropic-version alias is not forwarded; only the config default is sent.
    assert_eq!(h(&seen[1], "anthropic-version"), Some("2023-06-01"));
    // A valid gateway key in any header other than x-gateway-key is a 401.
    let c = reqwest::Client::new();
    for (k, v) in [
        ("x-api-key", KEY.to_string()),
        ("authorization", format!("Bearer {KEY}")),
    ] {
        let r = c
            .post(format!("{gw}/v1/messages"))
            .header(k, v)
            .body(json!({"model": "shared"}).to_string())
            .send()
            .await
            .unwrap();
        assert_eq!(r.status(), StatusCode::UNAUTHORIZED, "{k}");
    }
    // The /v1/models query and override header are not read.
    let r = c
        .get(format!("{gw}/v1/models?format=anthropic"))
        .header("x-gateway-key", KEY)
        .header("x-gateway-model-list-format", "anthropic")
        .send()
        .await
        .unwrap();
    let v: Value = serde_json::from_str(&r.text().await.unwrap()).unwrap();
    assert_eq!(v["object"], "list");
    assert_eq!(up.seen().len(), 2);
}

#[tokio::test]
async fn connect_error_is_retried_once_after_150ms() {
    // Reserve a port, release it, and listen on it only after the first attempt has failed.
    let reserved = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = reserved.local_addr().unwrap();
    drop(reserved);
    let raw = json!({"host": "127.0.0.1", "port": 0, "providers": [
        {"name": "side", "type": "anthropic", "apikey": "k", "baseurl": format!("http://{addr}/cd"), "models": ["gpt-x"]}]});
    let settings = config::from_json(&raw, &keys_env).unwrap();
    let state = legacy_state(settings, &|_| None);
    let (tx, rx) = tokio::sync::oneshot::channel();
    tokio::spawn(app::serve(state, move |a| tx.send(a).unwrap()));
    let gw = format!("http://{}", rx.await.unwrap());
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(40)).await;
        let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
        let ok = || async { r#"{"ok":true}"# };
        axum::serve(listener, axum::Router::new().fallback(ok))
            .await
            .unwrap();
    });
    let t0 = std::time::Instant::now();
    let r = reqwest::Client::new()
        .post(format!("{gw}/v1/messages"))
        .header("x-gateway-key", KEY)
        .body(r#"{"model":"side/gpt-x"}"#)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status().as_u16(), 200);
    assert_eq!(r.text().await.unwrap(), r#"{"ok":true}"#);
    assert!(t0.elapsed() >= Duration::from_millis(150));
}

#[tokio::test]
async fn systemone_forwards_no_caller_header() {
    let up = stub(Script::new()).await;
    let gw = gateway(&up, &[]).await;
    let inbound = [
        ("openai-organization", "org-1"),
        ("openai-project", "proj-1"),
        ("anthropic-beta", "b1"),
        ("user-agent", "caller-ua"),
        ("x-api-key", "caller-key"),
    ];
    let body = json!({"model": "openrouter-decide/typesafe/jev", "state": "s"});
    let (s, _, _) = post(&gw, "/v1/systemone", &inbound, body).await;
    assert_eq!(s, 200);
    let seen = up.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(h(&seen[0], "content-type"), Some("application/json"));
    assert_eq!(h(&seen[0], "authorization"), Some("Bearer or-secret"));
    for (name, _) in inbound {
        assert_eq!(h(&seen[0], name), None, "{name} leaked upstream");
    }
    for name in ["anthropic-version", "x-gateway-key"] {
        assert_eq!(h(&seen[0], name), None, "{name} leaked upstream");
    }
}

#[tokio::test]
async fn relayed_number_spelling_is_kept_and_local_clamp_still_applies() {
    let up = stub(Script::new()).await;
    let gw = gateway(&up, &[]).await;
    let send = |path: &'static str, body: &'static str| {
        let gw = gw.clone();
        async move {
            reqwest::Client::new()
                .post(format!("{gw}{path}"))
                .header("x-gateway-key", KEY)
                .body(body)
                .send()
                .await
                .unwrap()
                .status()
                .as_u16()
        }
    };
    let raw = r#"{"model":"openrouter/qwen/q","max_tokens":100,"t":0.10,"n":-0,"big":18446744073709551616}"#;
    assert_eq!(send("/v1/chat/completions", raw).await, 200);
    let local = r#"{"model":"claude-local/qwen","max_tokens":100000,"t":0.10,"messages":[]}"#;
    assert_eq!(send("/v1/messages", local).await, 200);
    let seen = up.seen();
    assert_eq!(seen.len(), 2);
    let relayed = std::str::from_utf8(&seen[0].raw).unwrap();
    for spelled in [
        r#""model":"qwen/q""#,
        r#""max_tokens":100"#,
        r#""t":0.10"#,
        r#""n":-0"#,
        r#""big":18446744073709551616"#,
    ] {
        assert!(relayed.contains(spelled), "{spelled} not in {relayed}");
    }
    assert_eq!(
        seen[1].body["max_tokens"],
        json!(32000),
        "clamp still clamps"
    );
    assert!(std::str::from_utf8(&seen[1].raw)
        .unwrap()
        .contains(r#""t":0.10"#));
}

#[tokio::test]
async fn reset_after_request_written_is_not_retried() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::io::AsyncReadExt;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let accepted = Arc::new(AtomicUsize::new(0));
    let count = accepted.clone();
    tokio::spawn(async move {
        loop {
            let (mut sock, _) = listener.accept().await.unwrap();
            count.fetch_add(1, Ordering::SeqCst);
            let (mut buf, mut got) = (vec![0u8; 8192], Vec::new());
            while !got.windows(4).any(|w| w == b"\r\n\r\n") {
                let n = sock.read(&mut buf).await.unwrap_or(0);
                if n == 0 {
                    break;
                }
                got.extend_from_slice(&buf[..n]);
            }
            drop(sock); // the request reached upstream; close without any response
        }
    });
    let raw = json!({"host": "127.0.0.1", "port": 0, "providers": [
        {"name": "side", "type": "anthropic", "apikey": "k", "baseurl": format!("http://{addr}/cd"), "models": ["gpt-x"]}]});
    let settings = config::from_json(&raw, &keys_env).unwrap();
    let state = legacy_state(settings, &|_| None);
    let (tx, rx) = tokio::sync::oneshot::channel();
    tokio::spawn(app::serve(state, move |a| tx.send(a).unwrap()));
    let gw = format!("http://{}", rx.await.unwrap());
    let r = reqwest::Client::new()
        .post(format!("{gw}/v1/messages"))
        .header("x-gateway-key", KEY)
        .body(r#"{"model":"side/gpt-x"}"#)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status().as_u16(), 502);
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(
        accepted.load(Ordering::SeqCst),
        1,
        "a written request is never retried"
    );
}
