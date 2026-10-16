//! `inference-dashboard --healthcheck`: the image's HEALTHCHECK probe (radar_tracker's
//! `health.rs`, plan Decision 8). std-only and blocking; never opens the DB. Healthy only when
//! the `/health` **body** says `status: "ok"` (root AGENTS.md: probe by body, never status).
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::{Duration, Instant};

/// Total budget for connect + request + response (the HEALTHCHECK timeout is 5 s).
pub const BUDGET: Duration = Duration::from_secs(3);

/// The response body (after the header block) says `{"status":"ok", ...}`.
pub fn body_is_ok(response: &[u8]) -> bool {
    let Some(at) = response.windows(4).position(|w| w == b"\r\n\r\n") else {
        return false;
    };
    let head = String::from_utf8_lossy(&response[..at]);
    let mut status = head.lines().next().unwrap_or("").split_whitespace();
    if !matches!(status.next(), Some(v) if v.starts_with("HTTP/1.")) {
        return false;
    }
    serde_json::from_slice::<serde_json::Value>(&response[at + 4..])
        .ok()
        .and_then(|v| v.get("status").and_then(|s| s.as_str()).map(|s| s == "ok"))
        .unwrap_or(false)
}

/// `GET /health` on `127.0.0.1:port`, read to EOF (`Connection: close`).
pub fn check(port: u16, budget: Duration) -> bool {
    let deadline = Instant::now() + budget;
    let left = || {
        deadline
            .saturating_duration_since(Instant::now())
            .max(Duration::from_millis(1))
    };
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut s) = TcpStream::connect_timeout(&addr, left()) else {
        return false;
    };
    let req =
        format!("GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    if s.set_write_timeout(Some(left())).is_err() || s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = Vec::new();
    let mut chunk = [0u8; 1024];
    while buf.len() < 64 * 1024 {
        if Instant::now() >= deadline || s.set_read_timeout(Some(left())).is_err() {
            return false;
        }
        match s.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(_) => return false,
        }
    }
    body_is_ok(&buf)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn healthy_only_by_body() {
        let ok = b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{\"status\":\"ok\",\"experimentCount\":3}";
        assert!(body_is_ok(ok));
        assert!(!body_is_ok(
            b"HTTP/1.1 200 OK\r\n\r\n{\"status\":\"degraded\"}"
        ));
        assert!(!body_is_ok(b"HTTP/1.1 200 OK\r\n\r\nInternal Server Error"));
        assert!(!body_is_ok(b"HTTP/1.1 200 OK\r\n"));
        assert!(!body_is_ok(b"garbage\r\n\r\n{\"status\":\"ok\"}"));
    }

    #[test]
    fn closed_port_is_unhealthy() {
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        assert!(!check(port, Duration::from_millis(300)));
    }
}
