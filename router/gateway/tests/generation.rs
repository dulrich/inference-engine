//! Two router generations on one inherited listening socket (plan
//! `live-cutover-router-and-sidecar`, "The generation contract"). The test owns ONE listening
//! socket (ephemeral port, loopback) and hands it to each generation as fd 3 with
//! LISTEN_FDS=1 and LISTEN_PID set to the generation's own pid, the way systemd does: a `sh`
//! gate sets LISTEN_PID=$$ and `exec`s the binary (exec keeps the pid). The test's own copy
//! never accepts and is closed before the retire. Upstream is an in-test fixture.

use axum::body::{Body, Bytes};
use axum::response::Response;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_inference-router");
const KEY: &str = "gen-test-key";
const GATE: &str = "LISTEN_PID=$$; LISTEN_FDS=1; export LISTEN_PID LISTEN_FDS; exec \"$@\"";
const CHUNKS: usize = 10;

struct TempDir(PathBuf);
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Fixture upstream: `{"slow": ms}` streams `CHUNKS` SSE events `ms` apart; anything else
/// answers one JSON body at once.
async fn upstream() -> std::net::SocketAddr {
    let handler = |body: Bytes| async move {
        let v: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
        match v.get("slow").and_then(Value::as_u64) {
            Some(ms) => {
                let s = futures_util::stream::unfold(0usize, move |i| async move {
                    if i == CHUNKS {
                        return None;
                    }
                    tokio::time::sleep(Duration::from_millis(ms)).await;
                    let ev = format!("event: ping\ndata: {{\"n\":{i}}}\n\n");
                    Some((Ok::<_, std::io::Error>(Bytes::from(ev)), i + 1))
                });
                Response::builder()
                    .header("content-type", "text/event-stream")
                    .body(Body::from_stream(s))
                    .unwrap()
            }
            None => Response::builder()
                .header("content-type", "application/json")
                .body(Body::from("{\"ok\":true}"))
                .unwrap(),
        }
    };
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(
            l,
            axum::Router::new().fallback(axum::routing::post(handler)),
        )
        .await
        .unwrap()
    });
    addr
}

fn home(up: std::net::SocketAddr) -> TempDir {
    static N: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "inference-router-gen-{}-{}-{}",
        std::process::id(),
        N.fetch_add(1, Ordering::SeqCst),
        up.port()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    // Port 1 is never bound: an inherited socket must win over HOST/PORT.
    let cfg = json!({"host": "127.0.0.1", "port": 1, "providers": [
        {"name": "side", "type": "anthropic", "apikey": "k", "baseurl": format!("http://{up}/cd")}]});
    let cat =
        json!({"schema": "model-catalog-v1", "providers": {"side": {"models": [{"id": "gpt-x"}]}}});
    std::fs::write(dir.join("gateway.config.json"), cfg.to_string()).unwrap();
    std::fs::write(dir.join("catalog.json"), cat.to_string()).unwrap();
    TempDir(dir)
}

struct Gen {
    child: Child,
    err: Arc<Mutex<String>>,
}

impl Gen {
    fn spawn(dir: &TempDir, listener: &std::net::TcpListener, name: &str) -> Gen {
        let fd = listener.as_raw_fd();
        let mut cmd = Command::new("sh");
        cmd.args(["-c", GATE, "sh", BIN])
            .env_clear()
            .env("PATH", std::env::var("PATH").unwrap_or_default())
            .env("INFERENCE_ROUTER_GATEWAY_KEY", KEY)
            .env("INFERENCE_ROUTER_ACTIVATION", name)
            .env("GATEWAY_CONFIG_PATH", dir.0.join("gateway.config.json"))
            .env("GATEWAY_CATALOG_PATH", dir.0.join("catalog.json"))
            .env("ROUTER_TELEMETRY_DIR", dir.0.join("logs"))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        // SAFETY: dup2 is async-signal-safe; the duplicate at fd 3 has no close-on-exec flag.
        unsafe {
            cmd.pre_exec(move || {
                if libc::dup2(fd, 3) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = cmd.spawn().unwrap();
        let err = Arc::new(Mutex::new(String::new()));
        let (e2, stderr) = (err.clone(), child.stderr.take().unwrap());
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                let mut e = e2.lock().unwrap();
                e.push_str(&line);
                e.push('\n');
            }
        });
        Gen { child, err }
    }

    fn stderr(&self) -> String {
        self.err.lock().unwrap().clone()
    }

    async fn wait_for(&self, needle: &str) {
        let t = Instant::now();
        while !self.stderr().contains(needle) {
            assert!(
                t.elapsed() < Duration::from_secs(10),
                "no {needle:?} in: {}",
                self.stderr()
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    fn signal(&self, sig: i32) {
        // SAFETY: the pid is our own child (the gate exec'd the binary in place).
        unsafe {
            libc::kill(self.child.id() as i32, sig);
        }
    }

    async fn exit(&mut self, within: Duration) -> (Option<i32>, Duration) {
        let t = Instant::now();
        loop {
            if let Some(s) = self.child.try_wait().unwrap() {
                return (s.code(), t.elapsed());
            }
            assert!(t.elapsed() < within, "no exit within {within:?}");
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }
}

impl Drop for Gen {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

async fn activation(c: &reqwest::Client, base: &str) -> Result<String, reqwest::Error> {
    let text = c.get(format!("{base}/health")).send().await?.text().await?;
    let v: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
    Ok(v["activation"].as_str().unwrap_or_default().to_string())
}

fn fresh_client() -> reqwest::Client {
    reqwest::Client::builder()
        .no_proxy()
        .pool_max_idle_per_host(0)
        .build()
        .unwrap()
}

async fn start_stream(base: &str, ms: u64) -> tokio::task::JoinHandle<Result<String, String>> {
    let resp = fresh_client()
        .post(format!("{base}/v1/messages"))
        .header("x-gateway-key", KEY)
        .body(json!({"model": "side/gpt-x", "stream": true, "slow": ms}).to_string())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status().as_u16(), 200);
    tokio::spawn(async move { resp.text().await.map_err(|e| e.to_string()) })
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn cutover_keeps_the_stream_and_hands_over_to_the_new_generation() {
    let up = upstream().await;
    let dir = home(up);
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let mut a = Gen::spawn(&dir, &listener, "gen-a");
    a.wait_for("listening on").await;
    let stream = start_stream(&base, 200).await;
    let b = Gen::spawn(&dir, &listener, "gen-b");
    b.wait_for("listening on").await;
    drop(listener);

    // Background traffic across the whole cutover.
    let stop = Arc::new(AtomicBool::new(false));
    let fresh_fail = Arc::new(Mutex::new(Vec::<String>::new()));
    let reused_fail = Arc::new(Mutex::new(Vec::<String>::new()));
    let (s1, f1, b1) = (stop.clone(), fresh_fail.clone(), base.clone());
    let fresh = tokio::spawn(async move {
        let mut n = 0;
        while !s1.load(Ordering::SeqCst) {
            if let Err(e) = activation(&fresh_client(), &b1).await {
                f1.lock().unwrap().push(format!("{e:?}"));
            }
            n += 1;
        }
        n
    });
    let (s2, f2, b2) = (stop.clone(), reused_fail.clone(), base.clone());
    let reused = tokio::spawn(async move {
        let c = reqwest::Client::builder().no_proxy().build().unwrap();
        let (mut n, mut retried) = (0, 0);
        while !s2.load(Ordering::SeqCst) {
            match activation(&c, &b2).await {
                Ok(_) => {}
                // The stated window: a connection-level error with no response byte on a
                // reused connection is retried, once; nothing else is.
                Err(e) if n > 0 && e.is_request() && !e.is_connect() && !e.is_timeout() => {
                    retried += 1;
                    if let Err(e) = activation(&c, &b2).await {
                        f2.lock().unwrap().push(format!("after retry: {e:?}"));
                    }
                }
                Err(e) => f2.lock().unwrap().push(format!("{e:?}")),
            }
            n += 1;
        }
        (n, retried)
    });
    tokio::time::sleep(Duration::from_millis(150)).await;

    a.signal(libc::SIGUSR1);
    a.wait_for("retiring (stopped accepting)").await;
    // Once A acknowledged, only B answers.
    for _ in 0..30 {
        assert_eq!(activation(&fresh_client(), &base).await.unwrap(), "gen-b");
    }
    let body = stream
        .await
        .unwrap()
        .expect("the in-flight stream completes");
    for i in 0..CHUNKS {
        assert!(
            body.contains(&format!("{{\"n\":{i}}}")),
            "chunk {i} missing: {body}"
        );
    }
    let (code, _) = a.exit(Duration::from_secs(10)).await;
    assert_eq!(code, Some(0), "A retired cleanly: {}", a.stderr());
    assert!(
        a.stderr().contains("inference-router: retired"),
        "{}",
        a.stderr()
    );
    assert!(
        a.stderr().contains("\"event\":\"retired\""),
        "{}",
        a.stderr()
    );

    stop.store(true, Ordering::SeqCst);
    let n_fresh = fresh.await.unwrap();
    let (n_reused, _retried) = reused.await.unwrap();
    assert!(
        n_fresh > 10 && n_reused > 10,
        "traffic ran: {n_fresh} / {n_reused}"
    );
    assert_eq!(
        *fresh_fail.lock().unwrap(),
        Vec::<String>::new(),
        "fresh connections never fail"
    );
    assert_eq!(
        *reused_fail.lock().unwrap(),
        Vec::<String>::new(),
        "only the stated window, retried"
    );
    drop(b);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn sigterm_wins_over_a_retire() {
    let up = upstream().await;
    let dir = home(up);
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let mut a = Gen::spawn(&dir, &listener, "gen-a");
    a.wait_for("listening on").await;
    drop(listener);
    // A stream far longer than the drain bound keeps the retire open.
    let _stream = start_stream(&base, 3000).await;
    a.signal(libc::SIGUSR1);
    a.wait_for("retiring (stopped accepting)").await;
    a.signal(libc::SIGTERM);
    let (code, took) = a.exit(Duration::from_secs(8)).await;
    assert!(
        took < Duration::from_millis(5500),
        "SIGTERM exit took {took:?}"
    );
    assert_eq!(code, Some(0), "{}", a.stderr());
    assert!(
        !a.stderr().contains("inference-router: retired"),
        "{}",
        a.stderr()
    );
}
