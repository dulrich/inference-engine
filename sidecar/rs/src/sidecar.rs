//! The composition root: the process-wide state (`World`), boot,
//! the two binds with the docker0 retry budget, and shutdown.
//!
//! Every piece of mutable state lives in ONE `World` behind one mutex, and every step that
//! must not interleave with another is one critical section. Nothing awaits while holding the
//! lock; every `await` is a lock release. Shared objects (sessions, sinks, joiners, response
//! handles) live in id-keyed maps in `World` and are referenced by id.

use crate::config::{Config, Pinned, BUILD_ID, VERSION};
use crate::errors::SidecarError;
use crate::logging::Logger;
use crate::outbox::OutboxWriter;
use crate::persona::Persona;
use crate::session::{Joiner, Session};
use crate::sinks::Sink;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, Weak};
use tokio::net::TcpListener;

pub type Shared = Arc<Mutex<World>>;

#[derive(Default)]
pub struct Binds {
    pub expected: Vec<String>,
    pub bound: Vec<String>,
}

pub struct World {
    pub me: Weak<Mutex<World>>,
    pub cfg: Config,
    pub pinned: Pinned,
    pub logger: Logger,
    pub outbox: OutboxWriter,
    pub outbox_spawned: HashSet<String>,
    pub personas: Vec<Persona>,
    pub sessions: HashMap<u64, Session>,
    pub sinks: HashMap<u64, Sink>,
    pub joiners: HashMap<u64, Joiner>,
    pub binds: Binds,
    /// The bind-extra retry task, aborted by `close()`.
    pub bind_retry: Option<tokio::task::AbortHandle>,
    /// Set once SIGUSR1 began a retire (`/healthz` reports it on a still-open connection).
    pub retiring: bool,
    next_id: u64,
}

impl World {
    pub fn id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }

    pub fn shared(&self) -> Weak<Mutex<World>> {
        self.me.clone()
    }

    /// `logger.write(kind, obj)` with an object literal built by `json!`.
    pub fn log(&mut self, kind: &str, obj: Value) {
        let m = match obj {
            Value::Object(m) => m,
            _ => Map::new(),
        };
        self.logger.write(kind, &m);
    }

    pub fn persona_index(&self, name: &str) -> Option<usize> {
        self.personas.iter().position(|p| p.name == name)
    }

    /// Sessions nothing can reach any more (not keyed, not on a bus, no request driving or
    /// joined to them) are dropped.
    pub fn gc_sessions(&mut self) {
        self.retire_displaced();
        let mut live: HashSet<u64> = HashSet::new();
        for p in &self.personas {
            live.extend(p.sessions.values().copied());
            live.extend(p.bus.values().copied());
        }
        self.sessions.retain(|id, s| {
            live.contains(id) || s.owner.is_some() || !s.joiners.is_empty() || s.resolve.is_some()
        });
    }
}

impl World {
    /// Retires every displaced session whose owner, joiners and resolver have settled:
    /// closed and off the bus, so `gc_sessions` can drop it.
    fn retire_displaced(&mut self) {
        let done: Vec<u64> = self
            .sessions
            .iter()
            .filter(|(id, s)| {
                s.displaced
                    && !s.closed
                    && !s.in_flight()
                    && self.personas[s.persona].session(&s.key) != Some(**id)
            })
            .map(|(id, _)| *id)
            .collect();
        for sid in done {
            let Some(s) = self.sessions.get(&sid) else {
                continue;
            };
            let (pi, key) = (s.persona, s.key.clone());
            let tid = s.thread_id.as_ref().map_or(Value::Null, Value::from);
            crate::session::close(self, sid, "session displaced");
            let name = self.personas[pi].name.clone();
            self.log(
                "session-retired",
                json!({ "persona": name, "key": key, "threadId": tid }),
            );
        }
    }
}

pub fn lock(w: &Mutex<World>) -> MutexGuard<'_, World> {
    w.lock().unwrap_or_else(|e| e.into_inner())
}

/// Upgrade a weak handle and lock, or `None` when the world is gone.
pub fn with_world<R>(w: &Weak<Mutex<World>>, f: impl FnOnce(&mut World) -> R) -> Option<R> {
    let a = w.upgrade()?;
    let mut g = lock(&a);
    Some(f(&mut g))
}

/// `n` bytes from the kernel CSPRNG (`getrandom(2)`).
pub fn random_bytes(n: usize) -> std::io::Result<Vec<u8>> {
    let mut buf = vec![0u8; n];
    let mut got = 0;
    while got < n {
        // SAFETY: the pointer/length pair is the unfilled tail of `buf`.
        let r = unsafe { libc::getrandom(buf[got..].as_mut_ptr().cast(), n - got, 0) };
        if r < 0 {
            let e = std::io::Error::last_os_error();
            if e.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(e);
        }
        got += r as usize;
    }
    Ok(buf)
}

/// `n` random bytes as lowercase hex (`crypto.randomBytes(n).toString("hex")`). `start`
/// refuses to boot when the random source fails, and once it has answered, getrandom(2)
/// does not fail for reads of up to 256 bytes, so a failure here is an invariant breach.
pub fn random_hex(n: usize) -> String {
    let buf = random_bytes(n).expect("getrandom(2) failed after the startup probe");
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

/// A bind failure: the address and the native `io::Error` text.
pub fn listen_error(e: &std::io::Error, host: &str, port: u16) -> String {
    format!("listen {host}:{port}: {e}")
}

pub struct Handle {
    pub world: Shared,
    pub port: u16,
    pub host: String,
    pub persona_names: Vec<String>,
    shutdown: Vec<tokio::sync::oneshot::Sender<()>>,
    servers: Arc<Mutex<Vec<tokio::task::JoinHandle<()>>>>,
    shutdown_tx: Arc<Mutex<Vec<tokio::sync::oneshot::Sender<()>>>>,
    /// Listening sockets this process still holds (each `Counted` decrements on drop).
    open_listeners: Arc<AtomicUsize>,
}

/// A listener that counts itself: axum drops it when the graceful-shutdown signal ends its
/// accept loop, so "the count reached 0" is the observable "stopped accepting".
struct Counted {
    inner: TcpListener,
    open: Arc<AtomicUsize>,
}

impl Drop for Counted {
    fn drop(&mut self) {
        self.open.fetch_sub(1, Ordering::SeqCst);
    }
}

impl axum::serve::Listener for Counted {
    type Io = tokio::net::TcpStream;
    type Addr = std::net::SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        axum::serve::Listener::accept(&mut self.inner).await
    }

    fn local_addr(&self) -> std::io::Result<Self::Addr> {
        self.inner.local_addr()
    }
}

/// systemd socket activation (`sd_listen_fds(3)`): when `LISTEN_PID` is this process and
/// `LISTEN_FDS` is n > 0, fds 3..3+n are listening sockets handed over by the socket unit.
/// The three variables are removed from the environment so no child inherits them, and
/// each fd is marked close-on-exec. Call before any thread starts (it edits the environment).
pub fn take_inherited_listeners() -> Vec<std::net::TcpListener> {
    let pid = std::env::var("LISTEN_PID").ok();
    let n = std::env::var("LISTEN_FDS").ok();
    for k in ["LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"] {
        std::env::remove_var(k);
    }
    let me = std::process::id().to_string();
    if pid.as_deref().map(str::trim) != Some(me.as_str()) {
        return Vec::new();
    }
    let n: i32 = n.and_then(|v| v.trim().parse().ok()).unwrap_or(0);
    let mut out = Vec::new();
    for fd in 3..3 + n.clamp(0, 64) {
        use std::os::fd::FromRawFd;
        // SAFETY: systemd's contract hands these fds to this pid; each is taken exactly once.
        unsafe {
            libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC);
        }
        let l = unsafe { std::net::TcpListener::from_raw_fd(fd) };
        out.push(l);
    }
    out
}

static NOTIFY_SOCKET: OnceLock<Option<String>> = OnceLock::new();

/// Takes `NOTIFY_SOCKET` out of the environment (children must not inherit it). Call before
/// any thread starts.
pub fn capture_notify_socket() {
    let v = std::env::var("NOTIFY_SOCKET")
        .ok()
        .filter(|s| !s.is_empty());
    std::env::remove_var("NOTIFY_SOCKET");
    let _ = NOTIFY_SOCKET.set(v);
}

/// A dependency-free `sd_notify(3)`: one datagram to `NOTIFY_SOCKET` (a path, or `@abstract`).
/// A no-op when the process was not started by a `Type=notify` unit.
pub fn sd_notify(msg: &str) {
    let Some(Some(path)) = NOTIFY_SOCKET.get() else {
        return;
    };
    let Ok(sock) = std::os::unix::net::UnixDatagram::unbound() else {
        return;
    };
    if let Some(name) = path.strip_prefix('@') {
        use std::os::linux::net::SocketAddrExt;
        if let Ok(addr) = std::os::unix::net::SocketAddr::from_abstract_name(name.as_bytes()) {
            let _ = sock.send_to_addr(msg.as_bytes(), &addr);
        }
    } else {
        let _ = sock.send_to(msg.as_bytes(), path);
    }
}

async fn bind(host: &str, port: u16) -> Result<TcpListener, String> {
    TcpListener::bind((host, port))
        .await
        .map_err(|e| listen_error(&e, host, port))
}

fn serve(
    world: &Shared,
    listener: TcpListener,
    servers: &Arc<Mutex<Vec<tokio::task::JoinHandle<()>>>>,
    shutdown_tx: &Arc<Mutex<Vec<tokio::sync::oneshot::Sender<()>>>>,
    open: &Arc<AtomicUsize>,
) {
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    let app = crate::http::router(world.clone());
    open.fetch_add(1, Ordering::SeqCst);
    let listener = Counted {
        inner: listener,
        open: open.clone(),
    };
    let task = tokio::spawn(async move {
        let _ = axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                let _ = rx.await;
            })
            .await;
    });
    servers.lock().unwrap_or_else(|e| e.into_inner()).push(task);
    shutdown_tx
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .push(tx);
}

fn bind_of(l: &TcpListener) -> String {
    l.local_addr().map(|a| a.to_string()).unwrap_or_default()
}

/// Boots the sidecar: personas, both binds, the shared state.
pub async fn start(cfg: Config, pinned: Pinned) -> Result<Handle, String> {
    start_with(cfg, pinned, Vec::new()).await
}

/// `start`, serving on `inherited` listening sockets when there are any (systemd socket
/// activation): then the configured port and the extra bind are ignored entirely.
pub async fn start_with(
    cfg: Config,
    pinned: Pinned,
    inherited: Vec<std::net::TcpListener>,
) -> Result<Handle, String> {
    if cfg.key.is_empty() {
        return Err("SIDECAR_KEY is required".into());
    }
    random_bytes(16).map_err(|e| format!("the OS random source failed: {e}"))?;
    let logger = Logger::new(cfg.log_dir.clone(), f64::from(cfg.log_keep_days));
    let outbox = OutboxWriter::new(&cfg.outbox_path);
    let names = cfg.personas.clone();
    let world: Shared = Arc::new_cyclic(|me| {
        let mut personas = Vec::new();
        for n in &names {
            if !personas.iter().any(|p: &Persona| &p.name == n) {
                personas.push(Persona::new(n.clone(), cfg.backoff_ms));
            }
        }
        Mutex::new(World {
            me: me.clone(),
            cfg,
            pinned,
            logger,
            outbox,
            outbox_spawned: HashSet::new(),
            personas,
            sessions: HashMap::new(),
            sinks: HashMap::new(),
            joiners: HashMap::new(),
            binds: Binds::default(),
            bind_retry: None,
            retiring: false,
            next_id: 0,
        })
    });
    let (port_cfg, bind_extra, retry_ms, budget_ms) = {
        let w = lock(&world);
        (
            w.cfg.port,
            w.cfg.bind_extra.clone(),
            w.cfg.bind_retry_ms,
            w.cfg.bind_retry_budget_ms,
        )
    };
    let servers = Arc::new(Mutex::new(Vec::new()));
    let shutdown_tx = Arc::new(Mutex::new(Vec::new()));
    let open = Arc::new(AtomicUsize::new(0));

    if !inherited.is_empty() {
        let mut first: Option<std::net::SocketAddr> = None;
        for l in inherited {
            l.set_nonblocking(true)
                .map_err(|e| format!("inherited socket: {e}"))?;
            let l = TcpListener::from_std(l).map_err(|e| format!("inherited socket: {e}"))?;
            if first.is_none() {
                first = l.local_addr().ok();
            }
            {
                let mut w = lock(&world);
                let b = bind_of(&l);
                w.binds.expected.push(b.clone());
                w.binds.bound.push(b);
            }
            serve(&world, l, &servers, &shutdown_tx, &open);
        }
        let addr = first.ok_or("inherited socket has no local address")?;
        return Ok(finish(
            world,
            addr.ip().to_string(),
            addr.port(),
            servers,
            shutdown_tx,
            open,
            true,
        ));
    }

    let want = port_cfg;
    let primary = bind("127.0.0.1", want).await?;
    let port = primary.local_addr().map(|a| a.port()).unwrap_or(want);
    {
        let mut w = lock(&world);
        let b = bind_of(&primary);
        w.binds.expected.push(b.clone());
        w.binds.bound.push(b);
    }
    serve(&world, primary, &servers, &shutdown_tx, &open);

    if !bind_extra.is_empty() {
        let target = format!("{bind_extra}:{port}");
        lock(&world).binds.expected.push(target);
        match bind(&bind_extra, port).await {
            Ok(l) => {
                lock(&world).binds.bound.push(bind_of(&l));
                serve(&world, l, &servers, &shutdown_tx, &open);
            }
            Err(first) => {
                let started = std::time::Instant::now();
                lock(&world).log(
                    "bind-extra-retrying",
                    json!({ "host": bind_extra, "port": port, "err": first, "budgetMs": budget_ms }),
                );
                let (wd, sv, st, op) = (
                    world.clone(),
                    servers.clone(),
                    shutdown_tx.clone(),
                    open.clone(),
                );
                let task = tokio::spawn(async move {
                    let mut attempts: u64 = 1;
                    let mut delay = retry_ms.max(1);
                    loop {
                        tokio::time::sleep(std::time::Duration::from_millis(delay)).await;
                        attempts += 1;
                        match bind(&bind_extra, port).await {
                            Ok(l) => {
                                {
                                    let mut w = lock(&wd);
                                    w.binds.bound.push(bind_of(&l));
                                    let el = started.elapsed().as_millis() as u64;
                                    w.log(
                                        "bind-extra-recovered",
                                        json!({ "host": bind_extra, "port": port, "attempts": attempts, "elapsedMs": el }),
                                    );
                                }
                                serve(&wd, l, &sv, &st, &op);
                                return;
                            }
                            Err(e) => {
                                let el = started.elapsed().as_millis() as u64;
                                if el + delay > budget_ms {
                                    lock(&wd).log(
                                        "bind-extra-failed",
                                        json!({ "host": bind_extra, "port": port, "attempts": attempts, "elapsedMs": el, "err": e }),
                                    );
                                    return;
                                }
                                delay = delay.saturating_mul(2).min(5000);
                            }
                        }
                    }
                });
                lock(&world).bind_retry = Some(task.abort_handle());
            }
        }
    }

    Ok(finish(
        world,
        "127.0.0.1".into(),
        port,
        servers,
        shutdown_tx,
        open,
        false,
    ))
}

type Tasks = Arc<Mutex<Vec<tokio::task::JoinHandle<()>>>>;
type Signals = Arc<Mutex<Vec<tokio::sync::oneshot::Sender<()>>>>;

fn finish(
    world: Shared,
    host: String,
    port: u16,
    servers: Tasks,
    shutdown_tx: Signals,
    open_listeners: Arc<AtomicUsize>,
    inherited: bool,
) -> Handle {
    let persona_names = {
        let mut w = lock(&world);
        let names: Vec<String> = w.personas.iter().map(|p| p.name.clone()).collect();
        let row = json!({
            "port": port, "binds": w.binds.bound, "expected": w.binds.expected,
            "personas": names, "logDir": w.cfg.log_dir, "keepDays": w.cfg.log_keep_days,
            "inherited": inherited, "activation": w.cfg.activation, "build": BUILD_ID,
        });
        w.log("listen", row);
        names
    };
    Handle {
        world,
        port,
        host,
        persona_names,
        shutdown: Vec::new(),
        servers,
        shutdown_tx,
        open_listeners,
    }
}

impl Handle {
    /// The stdout line printed once both binds are up.
    pub fn banner(&self) -> String {
        format!(
            "codex-sidecar {VERSION} listening on {}:{} personas={}\n",
            self.host,
            self.port,
            self.persona_names.join(",")
        )
    }

    /// Retire, step 1: flag the world retiring and end every accept loop. Each server stops
    /// accepting, closes its idle keep-alive connections and closes a busy one after its
    /// in-flight response (hyper graceful shutdown), so no further HTTP request is admitted.
    /// Personas are untouched: in-flight turns keep their child.
    pub fn begin_retire(&mut self) {
        {
            let mut w = lock(&self.world);
            w.retiring = true;
            if let Some(t) = w.bind_retry.take() {
                t.abort();
            }
        }
        self.shutdown
            .extend(std::mem::take(&mut *lock_vec(&self.shutdown_tx)));
        for tx in self.shutdown.drain(..) {
            let _ = tx.send(());
        }
    }

    /// Resolves once every listening socket this process held is closed: the retire
    /// acknowledgment point (`retiring` log row, `STATUS=retiring` to systemd).
    pub async fn stopped_accepting(&self) {
        while self.open_listeners.load(Ordering::SeqCst) > 0 {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    }

    /// Resolves once nothing is in flight: every connection closed (its server task ended)
    /// and every request's sink settled.
    pub async fn settled(&self) {
        loop {
            let servers_done = lock_vec(&self.servers).iter().all(|t| t.is_finished());
            if servers_done && lock(&self.world).sinks.is_empty() {
                return;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
    }

    /// One log row (used by `main` for the retire milestones).
    pub fn log(&self, kind: &str, obj: Value) {
        lock(&self.world).log(kind, obj);
    }

    /// `close()`: stop the retry, stop every persona, let in-flight requests land, cut the
    /// rest after 250 ms, then the `shutdown` row.
    pub async fn close(mut self) {
        {
            let mut w = lock(&self.world);
            if let Some(t) = w.bind_retry.take() {
                t.abort();
            }
            for i in 0..w.personas.len() {
                crate::persona::stop(&mut w, i);
            }
        }
        self.shutdown
            .extend(std::mem::take(&mut *lock_vec(&self.shutdown_tx)));
        for tx in self.shutdown.drain(..) {
            let _ = tx.send(());
        }
        let tasks: Vec<_> = std::mem::take(&mut *lock_vec(&self.servers));
        let all = futures_util::future::join_all(tasks);
        let _ = tokio::time::timeout(std::time::Duration::from_millis(250), all).await;
        let mut w = lock(&self.world);
        w.log("shutdown", json!({}));
        w.logger.close();
    }
}

fn lock_vec<T>(m: &Mutex<Vec<T>>) -> MutexGuard<'_, Vec<T>> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// `SidecarError` shorthand.
pub fn err(status: u16, t: &str, m: impl Into<String>) -> SidecarError {
    SidecarError::new(status, t, m)
}
