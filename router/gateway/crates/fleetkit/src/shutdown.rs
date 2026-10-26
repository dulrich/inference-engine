//! Fleet shutdown contract: SIGTERM or SIGINT starts a graceful drain, and a
//! std-thread watchdog bounds process exit to the drain deadline.
//!
//! ```ignore
//! let shutdown = fleetkit::shutdown::install(fleetkit::shutdown::DEFAULT_DRAIN);
//! let server = axum::serve(listener, app).with_graceful_shutdown(shutdown.triggered());
//! shutdown.drain(server).await;
//! ```
//!
//! The watchdog is armed only on trigger (never at install). It runs on a plain
//! std thread, so it fires even when the tokio runtime is blocked or being
//! dropped, and calls `std::process::exit(0)` once the deadline elapses.

use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::watch;

/// Default drain deadline: under docker's and systemd's 10 s stop budgets.
pub const DEFAULT_DRAIN: Duration = Duration::from_secs(5);

struct Inner {
    tx: watch::Sender<bool>,
    /// `None`: no hard-exit watchdog (tests, embedding).
    deadline: Option<Duration>,
    armed: AtomicBool,
}

/// Cheap cloneable shutdown handle; every clone observes the same trigger.
#[derive(Clone)]
pub struct Shutdown {
    inner: Arc<Inner>,
}

impl Shutdown {
    /// A handle whose trigger arms a hard-exit watchdog at `deadline`.
    /// Installs no signal handlers; see [`install`].
    pub fn new(deadline: Duration) -> Self {
        Self::build(Some(deadline))
    }

    /// A handle with no watchdog: triggering never exits the process.
    pub fn without_watchdog() -> Self {
        Self::build(None)
    }

    fn build(deadline: Option<Duration>) -> Self {
        let (tx, _rx) = watch::channel(false);
        Self {
            inner: Arc::new(Inner {
                tx,
                deadline,
                armed: AtomicBool::new(false),
            }),
        }
    }

    /// The drain deadline, if this handle has a watchdog.
    pub fn deadline(&self) -> Option<Duration> {
        self.inner.deadline
    }

    /// Start shutdown (idempotent). The first call arms the watchdog.
    pub fn trigger(&self) {
        self.inner.tx.send_replace(true);
        if self.inner.armed.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Some(deadline) = self.inner.deadline {
            let spawned = std::thread::Builder::new()
                .name("fleetkit-shutdown-watchdog".into())
                .spawn(move || {
                    std::thread::sleep(deadline);
                    eprintln!(
                        "shutdown: drain deadline {:?} elapsed; forcing exit",
                        deadline
                    );
                    std::process::exit(0);
                });
            if let Err(e) = spawned {
                eprintln!("shutdown: could not spawn watchdog thread: {e}");
            }
        }
    }

    /// Whether shutdown has been triggered.
    pub fn is_triggered(&self) -> bool {
        *self.inner.tx.borrow()
    }

    /// Resolves once shutdown is triggered (immediately if it already was).
    /// `'static`, so it can be handed to `with_graceful_shutdown`.
    pub fn triggered(&self) -> impl Future<Output = ()> + Send + 'static {
        let mut rx = self.inner.tx.subscribe();
        async move {
            // The sender lives in `Inner`, which this future does not hold; a
            // dropped sender means no trigger can ever come, so pend forever.
            if rx.wait_for(|v| *v).await.is_err() {
                std::future::pending::<()>().await;
            }
        }
    }

    /// Await `server`; after a trigger, give it at most the drain deadline.
    /// Returns `Some(output)` if it completed, `None` if the deadline elapsed
    /// (the watchdog then exits the process). Logs which to stderr.
    pub async fn drain<F: Future>(&self, server: F) -> Option<F::Output> {
        let deadline = self.inner.deadline.unwrap_or(DEFAULT_DRAIN);
        let expired = {
            let triggered = self.triggered();
            async move {
                triggered.await;
                tokio::time::sleep(deadline).await;
            }
        };
        tokio::select! {
            out = server => {
                if self.is_triggered() {
                    eprintln!("shutdown: drain complete");
                }
                Some(out)
            }
            () = expired => {
                eprintln!("shutdown: drain deadline {deadline:?} elapsed with work outstanding");
                None
            }
        }
    }
}

/// Create a [`Shutdown`] with a watchdog at `deadline` and spawn the signal
/// listener (SIGTERM + SIGINT). Must be called inside a tokio runtime. The
/// handlers are registered before this returns, so no early signal is lost. If
/// the SIGTERM handler cannot be installed, falls back to SIGINT only (logged).
pub fn install(deadline: Duration) -> Shutdown {
    let handle = Shutdown::new(deadline);
    let listener = handle.clone();
    let signals = register_signals();
    tokio::spawn(async move {
        signals.recv().await;
        eprintln!("shutdown: signal received; draining (deadline {deadline:?})");
        listener.trigger();
    });
    handle
}

#[cfg(unix)]
struct Signals {
    term: Option<tokio::signal::unix::Signal>,
    int: Option<tokio::signal::unix::Signal>,
}

#[cfg(unix)]
fn register_signals() -> Signals {
    use tokio::signal::unix::{signal, SignalKind};
    let term = signal(SignalKind::terminate())
        .map_err(|e| eprintln!("shutdown: SIGTERM handler unavailable ({e}); SIGINT only"))
        .ok();
    let int = signal(SignalKind::interrupt())
        .map_err(|e| eprintln!("shutdown: SIGINT handler unavailable ({e})"))
        .ok();
    if term.is_none() && int.is_none() {
        eprintln!("shutdown: no signal handler installed; shutdown only via trigger()");
    }
    Signals { term, int }
}

#[cfg(unix)]
impl Signals {
    async fn recv(mut self) {
        async fn one(s: &mut Option<tokio::signal::unix::Signal>) {
            match s {
                Some(s) => {
                    s.recv().await;
                }
                None => std::future::pending::<()>().await,
            }
        }
        tokio::select! {
            () = one(&mut self.term) => {}
            () = one(&mut self.int) => {}
        }
    }
}

#[cfg(not(unix))]
struct Signals;

#[cfg(not(unix))]
fn register_signals() -> Signals {
    Signals
}

#[cfg(not(unix))]
impl Signals {
    async fn recv(self) {
        if let Err(e) = tokio::signal::ctrl_c().await {
            eprintln!("shutdown: SIGINT handler unavailable: {e}");
            std::future::pending::<()>().await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn trigger_resolves_triggered_for_all_clones() {
        let a = Shutdown::without_watchdog();
        let b = a.clone();
        let waiting = tokio::spawn(b.triggered());
        assert!(!a.is_triggered() && !b.is_triggered());
        a.trigger();
        waiting.await.unwrap();
        assert!(b.is_triggered());
        // Already-triggered: resolves immediately.
        b.triggered().await;
    }

    #[tokio::test(start_paused = true)]
    async fn drain_returns_finished_or_times_out() {
        let s = Shutdown::without_watchdog();
        s.trigger();
        assert_eq!(s.drain(async { 7 }).await, Some(7));
        let hung = s.drain(std::future::pending::<()>()).await;
        assert_eq!(hung, None);
    }
}
