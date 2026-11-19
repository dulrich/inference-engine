//! WP-5: an upstream body that fails after headers. Anthropic-shape SSE gets one terminal error
//! frame and a clean end; everything else keeps the plain error. The fixture upstream is a raw
//! loopback socket that writes a chunked response and drops the connection without the final
//! chunk, so the router's upstream read fails after the headers.

use futures_util::StreamExt;
use inference_router::{app, catalog, config, telemetry};
use serde_json::json;
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const KEY: &str = "cut-test-key";
const ERR_FRAME: &str = "event: error\ndata: {\"type\":\"error\",\"error\":{\"type\":\"api_error\",\"message\":\"upstream stream failed before completion\"}}\n\n";

/// Fixture upstream: every connection gets `content_type` and the `chunks`, then is dropped.
async fn cut_upstream(
    content_type: &'static str,
    chunks: Vec<&'static [u8]>,
) -> std::net::SocketAddr {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let (mut sock, _) = l.accept().await.unwrap();
            let chunks = chunks.clone();
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut tmp = [0u8; 4096];
                // Request head, then the body by content-length.
                let (head_end, want) = loop {
                    let n = sock.read(&mut tmp).await.unwrap_or(0);
                    if n == 0 {
                        return;
                    }
                    buf.extend_from_slice(&tmp[..n]);
                    if let Some(p) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8_lossy(&buf[..p]).to_ascii_lowercase();
                        let len = head
                            .lines()
                            .find_map(|l| l.strip_prefix("content-length:"))
                            .and_then(|v| v.trim().parse::<usize>().ok())
                            .unwrap_or(0);
                        break (p + 4, len);
                    }
                };
                while buf.len() < head_end + want {
                    let n = sock.read(&mut tmp).await.unwrap_or(0);
                    if n == 0 {
                        return;
                    }
                    buf.extend_from_slice(&tmp[..n]);
                }
                let head = format!(
                    "HTTP/1.1 200 OK\r\ncontent-type: {content_type}\r\ntransfer-encoding: chunked\r\n\r\n"
                );
                let _ = sock.write_all(head.as_bytes()).await;
                for c in chunks {
                    let _ = sock
                        .write_all(format!("{:x}\r\n", c.len()).as_bytes())
                        .await;
                    let _ = sock.write_all(c).await;
                    let _ = sock.write_all(b"\r\n").await;
                    let _ = sock.flush().await;
                    // Keep chunks distinct on the wire so a boundary is a real chunk boundary.
                    tokio::time::sleep(std::time::Duration::from_millis(20)).await;
                }
                // Dropped here without the terminating zero chunk: a body failure.
            });
        }
    });
    addr
}

async fn gateway(up: std::net::SocketAddr) -> String {
    let base = format!("http://{up}");
    let raw = json!({"host": "127.0.0.1", "port": 0, "providers": [
        {"name": "side", "type": "anthropic", "apikey": "k", "baseurl": format!("{base}/a")},
        {"name": "oai", "type": "openai_chat_completions", "apikey": "k", "baseurl": format!("{base}/o/v1")}]});
    let cat = json!({"schema": "model-catalog-v1", "providers": {
        "side": {"models": [{"id": "m"}]}, "oai": {"models": [{"id": "m"}]}}});
    let env = |k: &str| (k == "AUTH_STATIC_API_KEYS").then(|| KEY.to_string());
    let settings = config::from_json(&raw, &env).unwrap();
    let parsed = catalog::check(&settings.providers, cat.to_string().as_bytes()).unwrap();
    let holder = Arc::new(catalog::Holder::new(
        settings.providers.clone(),
        parsed,
        None,
    ));
    let state = app::state_with(settings, holder, telemetry::from_env(&env)).unwrap();
    let (tx, rx) = tokio::sync::oneshot::channel();
    tokio::spawn(app::serve(state, move |a| tx.send(a).unwrap()));
    format!("http://{}", rx.await.unwrap())
}

/// POST and read the body to its end: the bytes received and whether the read failed.
async fn read(gw: &str, path: &str, model: &str) -> (u16, String, bool) {
    let r = reqwest::Client::new()
        .post(format!("{gw}{path}"))
        .header("x-gateway-key", KEY)
        .body(json!({"model": model, "stream": true}).to_string())
        .send()
        .await
        .unwrap();
    let status = r.status().as_u16();
    let mut s = r.bytes_stream();
    let mut out = Vec::new();
    let mut failed = false;
    while let Some(c) = s.next().await {
        match c {
            Ok(b) => out.extend_from_slice(&b),
            Err(_) => {
                failed = true;
                break;
            }
        }
    }
    (status, String::from_utf8(out).unwrap(), failed)
}

const SSE: &str = "text/event-stream";

#[tokio::test]
async fn failure_mid_frame_terminates_the_frame_then_one_error_frame() {
    let up = cut_upstream(
        SSE,
        vec![
            b"event: message_start\ndata: {\"a\":1}\n\nevent: content_block_delta\nda",
            b"ta: {\"x\":",
        ],
    )
    .await;
    let gw = gateway(up).await;
    let (status, body, failed) = read(&gw, "/v1/messages", "side/m").await;
    assert_eq!(status, 200);
    assert!(!failed, "the client sees a clean end");
    assert_eq!(
        body,
        format!("event: message_start\ndata: {{\"a\":1}}\n\nevent: content_block_delta\ndata: {{\"x\":\n\n{ERR_FRAME}")
    );
}

#[tokio::test]
async fn failure_on_a_frame_boundary_adds_no_blank_frame() {
    let up = cut_upstream(SSE, vec![b"event: ping\ndata: {}\n", b"\n"]).await;
    let gw = gateway(up).await;
    let (_, body, failed) = read(&gw, "/v1/messages", "side/m").await;
    assert!(!failed);
    assert_eq!(body, format!("event: ping\ndata: {{}}\n\n{ERR_FRAME}"));
}

#[tokio::test]
async fn failure_after_message_stop_appends_nothing() {
    // The event name straddles two chunks; the response was complete, so it just ends.
    let up = cut_upstream(
        SSE,
        vec![
            b"event: ping\ndata: {}\n\nevent: message_",
            b"stop\ndata: {}\n\n",
        ],
    )
    .await;
    let gw = gateway(up).await;
    let (_, body, failed) = read(&gw, "/v1/messages", "side/m").await;
    assert!(!failed);
    assert_eq!(
        body,
        "event: ping\ndata: {}\n\nevent: message_stop\ndata: {}\n\n"
    );
}

#[tokio::test]
async fn non_sse_and_openai_route_failures_keep_the_plain_error() {
    let json_up = cut_upstream("application/json", vec![b"{\"partial\":"]).await;
    let gw = gateway(json_up).await;
    let (_, body, failed) = read(&gw, "/v1/messages", "side/m").await;
    assert!(failed, "a non-SSE body failure still errors the client");
    assert!(!body.contains("event: error"), "{body}");

    let sse_up = cut_upstream(SSE, vec![b"data: {\"x\":"]).await;
    let gw = gateway(sse_up).await;
    let (_, body, failed) = read(&gw, "/v1/chat/completions", "oai/m").await;
    assert!(failed, "the OpenAI route keeps today's behaviour");
    assert!(!body.contains("event: error"), "{body}");
}
