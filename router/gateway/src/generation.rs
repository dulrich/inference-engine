//! Host generations (plan `live-cutover-router-and-sidecar`, "The generation contract"): the
//! systemd socket hand-over, readiness, and the SIGUSR1 retire, local to this crate beside the
//! fleetkit `shutdown` handle (fleetkit is vendored and not edited). Copied from the
//! codex-sidecar's WP-1 implementation (`sidecar/rs/src/sidecar.rs`).

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, OnceLock};

/// The exit status of a retire that hit its deadline (a clean retire exits 0).
pub const DEADLINE_EXIT: u8 = 3;

/// systemd socket activation (`sd_listen_fds(3)`): when `LISTEN_PID` is this process and
/// `LISTEN_FDS` is n > 0, fds 3..3+n are listening sockets handed over by the socket unit.
/// The three variables are removed from the environment and each fd is marked close-on-exec.
/// Call before any thread starts (it edits the environment).
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

/// Takes `NOTIFY_SOCKET` out of the environment. Call before any thread starts.
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

/// A listener that counts itself and stops accepting on its own `stop` signal: once `stop`
/// is true it closes its socket (the count drops; with every count at 0 the generation holds
/// no listener, the retire acknowledgment) and never yields another connection. axum's
/// graceful shutdown is triggered only afterwards (`app::serve_generation`), after a grace
/// that lets a connection accepted just before the stop read its first request; triggering
/// it at once would close such a fresh connection unanswered.
pub struct Counted {
    inner: Option<tokio::net::TcpListener>,
    addr: std::io::Result<std::net::SocketAddr>,
    open: Arc<AtomicUsize>,
    stop: tokio::sync::watch::Receiver<bool>,
}

impl Counted {
    pub fn new(
        inner: tokio::net::TcpListener,
        open: &Arc<AtomicUsize>,
        stop: tokio::sync::watch::Receiver<bool>,
    ) -> Counted {
        open.fetch_add(1, Ordering::SeqCst);
        Counted {
            addr: inner.local_addr(),
            inner: Some(inner),
            open: open.clone(),
            stop,
        }
    }

    fn close(&mut self) {
        if self.inner.take().is_some() {
            self.open.fetch_sub(1, Ordering::SeqCst);
        }
    }
}

impl Drop for Counted {
    fn drop(&mut self) {
        self.close();
    }
}

impl axum::serve::Listener for Counted {
    type Io = tokio::net::TcpStream;
    type Addr = std::net::SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            if *self.stop.borrow() {
                self.close();
            }
            let Some(inner) = self.inner.as_mut() else {
                return std::future::pending().await;
            };
            let mut stop = self.stop.clone();
            tokio::select! {
                biased;
                _ = stop.wait_for(|s| *s) => continue,
                c = axum::serve::Listener::accept(inner) => return c,
            }
        }
    }

    fn local_addr(&self) -> std::io::Result<Self::Addr> {
        match &self.addr {
            Ok(a) => Ok(*a),
            Err(e) => Err(std::io::Error::new(e.kind(), e.to_string())),
        }
    }
}
