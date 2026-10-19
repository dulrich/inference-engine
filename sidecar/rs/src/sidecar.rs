//! `codex-sidecar.mjs` — the composition root: the process-wide `ctx` (here `World`), boot,
//! the two binds with the docker0 retry budget, and shutdown.
//!
//! Node is single-threaded, so every piece of mutable state lives in ONE `World` behind one
//! mutex, and every "synchronous JS section" is one critical section. Nothing awaits while
//! holding the lock; an `await` in Node is a lock release here. Objects that Node shares by
//! reference (sessions, sinks, joiners, response handles) live in id-keyed maps in `World`.

use crate::config::{Config, Pinned, VERSION};
use crate::errors::SidecarError;
use crate::logging::Logger;
use crate::outbox::OutboxWriter;
use crate::persona::Persona;
use crate::session::{Joiner, Session};
use crate::sinks::{Res, Sink};
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, MutexGuard, Weak};
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
    pub res: HashMap<u64, Res>,
    pub joiners: HashMap<u64, Joiner>,
    pub binds: Binds,
    pub bind_retry_gen: u64,
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
    /// joined to them) are dropped; Node's GC does this implicitly.
    pub fn gc_sessions(&mut self) {
        let mut live: HashSet<u64> = HashSet::new();
        for p in &self.personas {
            live.extend(p.sessions.iter().map(|(_, s)| *s));
            live.extend(p.bus.iter().map(|(_, s)| *s));
        }
        self.sessions.retain(|id, s| {
            live.contains(id) || s.owner.is_some() || !s.joiners.is_empty() || s.resolve.is_some()
        });
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

/// `n` random bytes as lowercase hex (`crypto.randomBytes(n).toString("hex")`).
pub fn random_hex(n: usize) -> String {
    use std::io::Read;
    let mut buf = vec![0u8; n];
    let ok = std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut buf))
        .is_ok();
    if !ok {
        use std::hash::{BuildHasher, Hasher};
        for (i, b) in buf.iter_mut().enumerate() {
            let mut h = std::collections::hash_map::RandomState::new().build_hasher();
            h.write_usize(i);
            *b = h.finish() as u8;
        }
    }
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

/// Node's `listen` error text (`listen EADDRINUSE: address already in use 127.0.0.2:4877`).
pub fn listen_error(e: &std::io::Error, host: &str, port: u16) -> String {
    use std::io::ErrorKind;
    let (code, text) = match e.kind() {
        ErrorKind::AddrInUse => ("EADDRINUSE", "address already in use"),
        ErrorKind::AddrNotAvailable => ("EADDRNOTAVAIL", "address not available"),
        ErrorKind::PermissionDenied => ("EACCES", "permission denied"),
        _ => return format!("listen {e} {host}:{port}"),
    };
    format!("listen {code}: {text} {host}:{port}")
}

pub struct Handle {
    pub world: Shared,
    pub port: u16,
    pub persona_names: Vec<String>,
    shutdown: Vec<tokio::sync::oneshot::Sender<()>>,
    servers: Arc<Mutex<Vec<tokio::task::JoinHandle<()>>>>,
    shutdown_tx: Arc<Mutex<Vec<tokio::sync::oneshot::Sender<()>>>>,
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
) {
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    let app = crate::http::router(world.clone());
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

/// `startSidecar(env)`.
pub async fn start(cfg: Config, pinned: Pinned) -> Result<Handle, String> {
    if cfg.key.is_empty() {
        return Err("SIDECAR_KEY is required".into());
    }
    let logger = Logger::new(cfg.log_dir.clone(), cfg.log_keep_days);
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
            res: HashMap::new(),
            joiners: HashMap::new(),
            binds: Binds::default(),
            bind_retry_gen: 0,
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

    let want = if (0.0..65536.0).contains(&port_cfg) {
        port_cfg as u16
    } else {
        return Err(format!(
            "options.port should be >= 0 and < 65536. Received {}.",
            crate::js::fmt_number(port_cfg)
        ));
    };
    let primary = bind("127.0.0.1", want).await?;
    let port = primary.local_addr().map(|a| a.port()).unwrap_or(want);
    {
        let mut w = lock(&world);
        let b = bind_of(&primary);
        w.binds.expected.push(b.clone());
        w.binds.bound.push(b);
    }
    serve(&world, primary, &servers, &shutdown_tx);

    if !bind_extra.is_empty() {
        let target = format!("{bind_extra}:{port}");
        lock(&world).binds.expected.push(target);
        match bind(&bind_extra, port).await {
            Ok(l) => {
                lock(&world).binds.bound.push(bind_of(&l));
                serve(&world, l, &servers, &shutdown_tx);
            }
            Err(first) => {
                let started = crate::js::now_ms();
                let gen = {
                    let mut w = lock(&world);
                    w.log(
                        "bind-extra-retrying",
                        json!({ "host": bind_extra, "port": port, "err": first, "budgetMs": crate::js::num(budget_ms) }),
                    );
                    w.bind_retry_gen += 1;
                    w.bind_retry_gen
                };
                let (wd, sv, st) = (world.clone(), servers.clone(), shutdown_tx.clone());
                tokio::spawn(async move {
                    let mut attempts: f64 = 1.0;
                    let mut delay = retry_ms.max(1.0);
                    loop {
                        tokio::time::sleep(std::time::Duration::from_millis(delay as u64)).await;
                        if lock(&wd).bind_retry_gen != gen {
                            return;
                        }
                        attempts += 1.0;
                        match bind(&bind_extra, port).await {
                            Ok(l) => {
                                {
                                    let mut w = lock(&wd);
                                    w.binds.bound.push(bind_of(&l));
                                    let el = crate::js::now_ms() - started;
                                    w.log(
                                        "bind-extra-recovered",
                                        json!({ "host": bind_extra, "port": port, "attempts": crate::js::num(attempts), "elapsedMs": crate::js::num(el) }),
                                    );
                                }
                                serve(&wd, l, &sv, &st);
                                return;
                            }
                            Err(e) => {
                                let el = crate::js::now_ms() - started;
                                if el + delay > budget_ms {
                                    lock(&wd).log(
                                        "bind-extra-failed",
                                        json!({ "host": bind_extra, "port": port, "attempts": crate::js::num(attempts), "elapsedMs": crate::js::num(el), "err": e }),
                                    );
                                    return;
                                }
                                delay = (delay * 2.0).min(5000.0);
                            }
                        }
                    }
                });
            }
        }
    }

    let persona_names = {
        let mut w = lock(&world);
        let names: Vec<String> = w.personas.iter().map(|p| p.name.clone()).collect();
        let row = json!({
            "port": port, "binds": w.binds.bound, "expected": w.binds.expected,
            "personas": names, "logDir": w.cfg.log_dir, "keepDays": crate::js::num(w.cfg.log_keep_days),
        });
        w.log("listen", row);
        names
    };
    Ok(Handle {
        world,
        port,
        persona_names,
        shutdown: Vec::new(),
        servers,
        shutdown_tx,
    })
}

impl Handle {
    /// The stdout line `codex-sidecar.mjs` prints once both binds are up.
    pub fn banner(&self) -> String {
        format!(
            "codex-sidecar {VERSION} listening on 127.0.0.1:{} personas={}\n",
            self.port,
            self.persona_names.join(",")
        )
    }

    /// `close()`: stop the retry, stop every persona, let in-flight requests land, cut the
    /// rest after 250 ms, then the `shutdown` row.
    pub async fn close(mut self) {
        {
            let mut w = lock(&self.world);
            w.bind_retry_gen += 1;
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

/// `new SidecarError(...)` shorthand.
pub fn err(status: u16, t: &str, m: impl Into<String>) -> SidecarError {
    SidecarError::new(status, t, m)
}
