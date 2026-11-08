//! E3: the live Tokio task count stays
//! flat across a ~2,000-delta SSE turn under the fake app-server — no task per frame.

use codex_sidecar::config::{read_config, Pinned};
use codex_sidecar::sidecar;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const DELTAS: usize = 2000;

fn bun() -> PathBuf {
    std::env::var_os("PATH")
        .into_iter()
        .flat_map(|p| std::env::split_paths(&p).collect::<Vec<_>>())
        .map(|d| d.join("bun"))
        .find(|p| p.is_file())
        .expect("`bun` on PATH (the fake app-server runs under bun)")
}

fn alive() -> usize {
    tokio::runtime::Handle::current()
        .metrics()
        .num_alive_tasks()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_long_sse_turn_keeps_the_live_task_count_flat() {
    let dir = std::env::temp_dir().join(format!("codex-sidecar-tasks-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let mut turn: Vec<serde_json::Value> = (0..DELTAS)
        .map(|_| serde_json::json!({ "text": "x" }))
        .collect();
    turn.push(serde_json::json!({ "complete": {} }));
    let scenario = serde_json::json!({ "defaultTurn": turn });
    std::fs::write(dir.join("codex-a.json"), scenario.to_string()).unwrap();
    let fake = Path::new(env!("CARGO_MANIFEST_DIR")).join("../test/fake-app-server.mjs");
    let d = dir.display();
    let env: HashMap<String, String> = [
        ("HOME", d.to_string()),
        ("SIDECAR_KEY", "test-key".into()),
        ("SIDECAR_PORT", "0".into()),
        ("SIDECAR_BIND_EXTRA", String::new()),
        ("SIDECAR_PERSONAS", "codex-a".into()),
        (
            "SIDECAR_APP_SERVER_CMD",
            format!(
                "{} {} {d}/{{persona}}.json",
                bun().display(),
                fake.display()
            ),
        ),
        ("SIDECAR_LOG_DIR", format!("{d}/logs")),
        ("SIDECAR_STATE_DIR", format!("{d}/state")),
        ("SIDECAR_PING_MS", "60000".into()),
        ("SIDECAR_TURN_TIMEOUT_MS", "20000".into()),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v))
    .collect();
    let cfg = read_config(&env).unwrap();
    let h = sidecar::start(cfg, Pinned::load().unwrap()).await.unwrap();

    // Warm-up turn on its own session: the persona child spawns on first use and keeps its
    // reader/writer tasks for life, so the baseline is taken once they exist. Sampling right
    // after boot both excludes them and races the boot tasks' registration.
    stream_turn(h.port, "00000000-0000-0000-0000-000000000000").await;
    let before = settled_alive(|_| true).await;

    let (text, peak) = stream_turn(h.port, "11111111-2222-3333-4444-555555555555").await;
    let deltas = text.matches("event: content_block_delta").count();
    assert_eq!(deltas, DELTAS, "every delta streamed");
    assert!(text.contains("event: message_stop"));
    let after = settled_alive(|n| n <= before + 2).await;
    eprintln!("live tokio tasks: before={before} peak={peak} after={after} deltas={deltas}");
    assert!(
        peak <= before + 16,
        "live tasks grew with frames: before={before} peak={peak}"
    );
    assert!(
        after <= before + 2,
        "tasks leaked: before={before} after={after}"
    );
    h.close().await;
    let _ = std::fs::remove_dir_all(&dir);
}

/// Waits until the live task count is steady (unchanged across 5 samples 10 ms apart) and
/// satisfies `done`, then returns it; at the 5 s deadline it returns the last sample so the
/// caller's assertion reports it. Replaces fixed sleeps: the count is read once it settles.
async fn settled_alive(done: impl Fn(usize) -> bool) -> usize {
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    let (mut last, mut steady) = (alive(), 0);
    while tokio::time::Instant::now() < deadline {
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        let n = alive();
        steady = if n == last { steady + 1 } else { 0 };
        last = n;
        if steady >= 5 && done(n) {
            break;
        }
    }
    last
}

/// One streamed `POST /codex-a/v1/messages` turn under `session_id`, read to EOF.
/// Returns the SSE text and the peak live task count seen while reading.
async fn stream_turn(port: u16, session_id: &str) -> (String, usize) {
    let body = serde_json::json!({
        "model": "gpt-5.6-luna", "max_tokens": 64, "stream": true,
        "system": [{ "type": "text", "text": "You are a helpful coding assistant." }],
        "messages": [{ "role": "user", "content": "hi" }],
        "metadata": { "user_id": serde_json::json!({ "session_id": session_id }).to_string() },
    })
    .to_string();
    let mut s = tokio::net::TcpStream::connect(("127.0.0.1", port))
        .await
        .unwrap();
    let head = format!(
        "POST /codex-a/v1/messages HTTP/1.1\r\nhost: x\r\nauthorization: Bearer test-key\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
        body.len()
    );
    s.write_all(head.as_bytes()).await.unwrap();
    s.write_all(body.as_bytes()).await.unwrap();
    let (mut text, mut buf, mut peak) = (String::new(), vec![0u8; 4096], alive());
    loop {
        let n = tokio::time::timeout(std::time::Duration::from_secs(30), s.read(&mut buf))
            .await
            .expect("stream stalled")
            .unwrap();
        peak = peak.max(alive());
        if n == 0 {
            break;
        }
        text.push_str(&String::from_utf8_lossy(&buf[..n]));
    }
    (text, peak)
}
