//! The hot-reloaded model catalog (plans/serving-control-redesign.md WP-2): rename-into-dir
//! reload, validate-then-swap with last-good, in-flight requests on their starting revision,
//! fatal startup, and the `check-catalog` subcommand. 127.0.0.1 only, fake credentials.

use axum::body::Body;
use axum::response::Response;
use inference_router::catalog::{self, Holder};
use inference_router::{app, config, telemetry};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const KEY: &str = "test-gateway-key";

fn temp_dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("inference-router-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn catalog_json(models: &[&str]) -> String {
    let models: Vec<Value> = models.iter().map(|m| json!({"id": m})).collect();
    json!({"schema": "model-catalog-v1", "providers": {"or-anthropic": {"models": models}}})
        .to_string()
}

/// Replace `dir/catalog.json` the way an editor or the dashboard does: write a sibling, rename.
fn replace(dir: &Path, text: &str) {
    let tmp = dir.join(".catalog.json.tmp");
    std::fs::write(&tmp, text).unwrap();
    std::fs::rename(&tmp, dir.join("catalog.json")).unwrap();
}

fn settings(base: &str) -> config::Settings {
    let raw = json!({"host": "127.0.0.1", "port": 0, "providers": [
        {"name": "or-anthropic", "type": "anthropic", "apiKeyEnv": "OR_KEY", "baseurl": format!("{base}/or")}
    ]});
    let env = |k: &str| match k {
        "AUTH_STATIC_API_KEYS" => Some(KEY.to_string()),
        "OR_KEY" => Some("or-secret".to_string()),
        _ => None,
    };
    config::from_json(&raw, &env).unwrap()
}

/// An upstream that answers every request after `delay`.
async fn slow_upstream(delay: Duration) -> String {
    let handler = move || async move {
        tokio::time::sleep(delay).await;
        Response::builder()
            .status(200)
            .header("content-type", "application/json")
            .body(Body::from("{\"type\":\"message\",\"content\":[]}"))
            .unwrap()
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, axum::Router::new().fallback(handler))
            .await
            .unwrap()
    });
    format!("http://{addr}")
}

async fn serve(state: Arc<app::AppState>) -> String {
    let (tx, rx) = tokio::sync::oneshot::channel();
    tokio::spawn(app::serve(state, move |a| tx.send(a).unwrap()));
    format!("http://{}", rx.await.unwrap())
}

async fn get_json(url: String) -> Value {
    let r = reqwest::Client::new()
        .get(url)
        .header("x-gateway-key", KEY)
        .send()
        .await
        .unwrap();
    serde_json::from_str(&r.text().await.unwrap()).unwrap()
}

async fn model_ids(gw: &str) -> Vec<String> {
    get_json(format!("{gw}/v1/models")).await["data"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["id"].as_str().unwrap().to_string())
        .collect()
}

/// Poll `/health` until `pred` holds, failing after 5 s.
async fn health_until(gw: &str, pred: impl Fn(&Value) -> bool) -> (Value, Duration) {
    let t0 = Instant::now();
    loop {
        let h = get_json(format!("{gw}/health")).await;
        if pred(&h) {
            return (h, t0.elapsed());
        }
        assert!(t0.elapsed() < Duration::from_secs(5), "not within 5 s: {h}");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

#[tokio::test]
async fn rename_into_directory_reloads_and_invalid_keeps_last_good() {
    let dir = temp_dir("reload");
    replace(&dir, &catalog_json(&["a"]));
    let path = dir.join("catalog.json").display().to_string();
    let env: HashMap<&str, String> = [
        ("GATEWAY_CATALOG_PATH", path),
        ("ROUTER_TELEMETRY_ENABLED", "false".into()),
    ]
    .into_iter()
    .collect();
    let get = move |k: &str| env.get(k).cloned();
    let gw = serve(app::state(settings("http://127.0.0.1:9"), &get).unwrap()).await;

    let h = get_json(format!("{gw}/health")).await;
    let rev1 = h["catalogRevision"].as_str().unwrap().to_string();
    assert_eq!(rev1, catalog::revision(catalog_json(&["a"]).as_bytes()));
    assert_eq!(h["catalogError"], Value::Null);
    assert_eq!(h["status"], "ok");
    assert_eq!(model_ids(&gw).await, ["or-anthropic/a"]);

    replace(&dir, &catalog_json(&["a", "b"]));
    let (h, _) = health_until(&gw, |h| h["catalogRevision"] != rev1.as_str()).await;
    let rev2 = h["catalogRevision"].as_str().unwrap().to_string();
    assert_eq!(h["catalogError"], Value::Null);
    assert_eq!(model_ids(&gw).await, ["or-anthropic/a", "or-anthropic/b"]);

    // Invalid: an empty list on a credentialed provider would be a wildcard.
    replace(&dir, &catalog_json(&[]));
    let (h, _) = health_until(&gw, |h| !h["catalogError"].is_null()).await;
    assert!(h["catalogError"]
        .as_str()
        .unwrap()
        .contains("empty on a credentialed provider"));
    assert_eq!(h["catalogRevision"], rev2.as_str());
    assert_eq!(model_ids(&gw).await, ["or-anthropic/a", "or-anthropic/b"]);

    // Restoring the live content clears the error without a swap.
    replace(&dir, &catalog_json(&["a", "b"]));
    let (h, _) = health_until(&gw, |h| h["catalogError"].is_null()).await;
    assert_eq!(h["catalogRevision"], rev2.as_str());
    let _ = std::fs::remove_dir_all(&dir);
}

struct Recorder(Arc<Mutex<Vec<String>>>);
struct NoopReq;
impl telemetry::RequestTelemetry for NoopReq {
    fn resolved(&mut self, _r: &telemetry::Resolution<'_>) {}
    fn upstream_chunk(&mut self, _chunk: &[u8]) {}
    fn finalize(self: Box<Self>, _o: &telemetry::Outcome) {}
}
impl telemetry::Telemetry for Recorder {
    fn request_start(
        &self,
        s: &telemetry::RequestStart<'_>,
    ) -> Box<dyn telemetry::RequestTelemetry> {
        self.0.lock().unwrap().push(s.catalog_revision.to_string());
        Box::new(NoopReq)
    }
}

async fn post_model(gw: &str, model: &str) -> (u16, String) {
    let r = reqwest::Client::new()
        .post(format!("{gw}/v1/messages"))
        .header("x-gateway-key", KEY)
        .body(json!({"model": model, "max_tokens": 1, "messages": []}).to_string())
        .send()
        .await
        .unwrap();
    (r.status().as_u16(), r.text().await.unwrap())
}

#[tokio::test]
async fn in_flight_request_finishes_on_the_catalog_it_started_with() {
    let up = slow_upstream(Duration::from_millis(1500)).await;
    let dir = temp_dir("inflight");
    replace(&dir, &catalog_json(&["a"]));
    let s = settings(&up);
    let holder = Arc::new(Holder::load(s.providers.clone(), dir.join("catalog.json")).unwrap());
    let revs = Arc::new(Mutex::new(Vec::new()));
    let state = app::state_with(s, holder.clone(), Arc::new(Recorder(revs.clone()))).unwrap();
    let gw = serve(state).await;
    let rev1 = holder.snapshot().revision.clone();

    let gw2 = gw.clone();
    let in_flight = tokio::spawn(async move { post_model(&gw2, "or-anthropic/a").await });
    tokio::time::sleep(Duration::from_millis(300)).await;
    // The new catalog drops `a` while the request for it is still waiting on the upstream.
    replace(&dir, &catalog_json(&["b"]));
    holder.poll_once();
    let rev2 = holder.snapshot().revision.clone();
    assert_ne!(rev1, rev2);
    assert_eq!(holder.error(), None);

    let (status, _) = in_flight.await.unwrap();
    assert_eq!(
        status, 200,
        "the in-flight request finished on its starting catalog"
    );
    let (status, body) = post_model(&gw, "or-anthropic/a").await;
    assert_eq!(status, 400, "{body}");
    assert!(
        body.contains("is not configured for target provider or-anthropic"),
        "{body}"
    );
    assert_eq!(*revs.lock().unwrap(), [rev1, rev2]);
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn missing_or_invalid_catalog_at_startup_is_fatal() {
    let dir = temp_dir("startup");
    let path = dir.join("catalog.json");
    let e = Holder::load(settings("http://x").providers, path.clone())
        .err()
        .unwrap();
    assert!(e.starts_with("read catalog"), "{e}");
    std::fs::write(&path, catalog_json(&[])).unwrap();
    let e = Holder::load(settings("http://x").providers, path)
        .err()
        .unwrap();
    assert!(e.contains("empty on a credentialed provider"), "{e}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn exposure_records_never_route_or_list() {
    let mut v: Value = serde_json::from_str(&catalog_json(&["a"])).unwrap();
    v["exposures"] =
        json!({"some-llama-key": {"consumers": ["openwebui"]}, "a": {"consumers": []}});
    let s = settings("http://127.0.0.1:9");
    let cat = catalog::check(&s.providers, v.to_string().as_bytes()).unwrap();
    assert_eq!(cat.exposures.len(), 2);
    let holder = Arc::new(Holder::new(s.providers.clone(), cat, None));
    let state = app::state_with(s, holder, Arc::new(Recorder(Arc::default()))).unwrap();
    let gw = serve(state).await;
    assert_eq!(model_ids(&gw).await, ["or-anthropic/a"]);
    let (status, body) = post_model(&gw, "or-anthropic/some-llama-key").await;
    assert_eq!(status, 400, "{body}");
    let (status, _) = post_model(&gw, "some-llama-key").await;
    assert_ne!(status, 200);
}

#[test]
fn check_catalog_exit_codes() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let committed = root.join("test/fixtures/catalog.router.fixture.json");
    let run = |path: &Path| {
        std::process::Command::new(env!("CARGO_BIN_EXE_inference-router"))
            .arg("check-catalog")
            .arg(path)
            // No gateway or provider keys: only the static config path.
            .env_clear()
            .env(
                "GATEWAY_CONFIG_PATH",
                root.join("test/fixtures/gateway.config.fixture.json"),
            )
            .output()
            .unwrap()
    };
    let ok = run(&committed);
    assert!(
        ok.status.success(),
        "{}",
        String::from_utf8_lossy(&ok.stderr)
    );
    let expected = catalog::revision(&std::fs::read(&committed).unwrap());
    assert_eq!(String::from_utf8_lossy(&ok.stdout).trim(), expected);

    let dir = temp_dir("check");
    let bad = dir.join("catalog.json");
    let mut v: Value = serde_json::from_slice(&std::fs::read(&committed).unwrap()).unwrap();
    v["providers"]["claude-local"]["models"] = json!([{"id": "@preset/x"}]);
    std::fs::write(&bad, v.to_string()).unwrap();
    let out = run(&bad);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("accepted only on"));
    assert!(!run(&dir.join("absent.json")).status.success());
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn shutdown_trigger_lets_serve_return_and_finishes_idle_cleanly() {
    let dir = temp_dir("shutdown");
    replace(&dir, &catalog_json(&["a"]));
    let path = dir.join("catalog.json").display().to_string();
    let env: HashMap<&str, String> = [
        ("GATEWAY_CATALOG_PATH", path),
        ("ROUTER_TELEMETRY_ENABLED", "false".into()),
    ]
    .into_iter()
    .collect();
    let get = move |k: &str| env.get(k).cloned();
    let state = app::state(settings("http://127.0.0.1:9"), &get).unwrap();
    let sd = fleetkit::shutdown::Shutdown::without_watchdog();
    let (tx, rx) = tokio::sync::oneshot::channel();
    let task = tokio::spawn(app::serve_until(state, sd.clone(), move |a| {
        tx.send(a).unwrap()
    }));
    let gw = format!("http://{}", rx.await.unwrap());
    assert_eq!(get_json(format!("{gw}/health")).await["status"], "ok");
    assert!(!task.is_finished());
    sd.trigger();
    let res = tokio::time::timeout(Duration::from_secs(5), task)
        .await
        .expect("serve returns after the trigger")
        .unwrap();
    assert!(res.is_ok());
}
