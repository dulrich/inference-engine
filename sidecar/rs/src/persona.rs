//! One `<persona> app-server` child and everything spoken over its stdio:
//! spawn, the newline-framed JSON-RPC codec, the pending-request table, the thread bus, the
//! sessions map, rate-limit posture, the security boundary (`trip_boundary`) and the backoff
//! restart. One reader task per child; every frame is handled under the world lock.

use crate::codex::{self, Note, RpcFrame, ThreadId, ToolCall};
use crate::config::VERSION;
use crate::errors::{map_upstream_error, rate_limit_exhaustion, SidecarError};
use crate::session::{self, is_set, Frame, Outcome};
use crate::sidecar::{err, with_world, World};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::process::Stdio;
use std::sync::{Mutex, Weak};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, oneshot};
use tokio::task::AbortHandle;
use tokio::time::Duration;

pub type RpcResult = Result<Value, SidecarError>;
pub type InitResult = Result<(), SidecarError>;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum InitSlot {
    None,
    Pending(u64),
    Ready(u64),
}

pub struct Persona {
    pub name: String,
    pub child_alive: bool,
    child_gen: u64,
    stdin: Option<mpsc::UnboundedSender<String>>,
    pid: Option<i32>,
    rpc_id: u64,
    pending: HashMap<u64, oneshot::Sender<RpcResult>>,
    /// threadId -> session id.
    pub bus: HashMap<ThreadId, u64>,
    /// session key -> session id.
    pub sessions: HashMap<String, u64>,
    init: InitSlot,
    init_waiters: HashMap<u64, Vec<oneshot::Sender<InitResult>>>,
    pub degraded: bool,
    degraded_reason: Option<Value>,
    backoff_ms: u64,
    /// Resets the backoff once a child has stayed up for 60 s; aborted on exit/stop.
    stable_timer: Option<AbortHandle>,
    pub hook_events: u64,
    pub mcp_events: u64,
    pub rate_limited: bool,
    stopped: bool,
}

/// The name of a signal number, keyed by libc's constants; else `SIG<n>`.
fn signal_name(sig: i32) -> String {
    let n = match sig {
        libc::SIGHUP => "SIGHUP",
        libc::SIGINT => "SIGINT",
        libc::SIGQUIT => "SIGQUIT",
        libc::SIGILL => "SIGILL",
        libc::SIGTRAP => "SIGTRAP",
        libc::SIGABRT => "SIGABRT",
        libc::SIGBUS => "SIGBUS",
        libc::SIGFPE => "SIGFPE",
        libc::SIGKILL => "SIGKILL",
        libc::SIGUSR1 => "SIGUSR1",
        libc::SIGSEGV => "SIGSEGV",
        libc::SIGUSR2 => "SIGUSR2",
        libc::SIGPIPE => "SIGPIPE",
        libc::SIGALRM => "SIGALRM",
        libc::SIGTERM => "SIGTERM",
        _ => return format!("SIG{sig}"),
    };
    n.to_string()
}

impl Persona {
    pub fn new(name: String, backoff_ms: u64) -> Self {
        Self {
            name,
            child_alive: false,
            child_gen: 0,
            stdin: None,
            pid: None,
            rpc_id: 1,
            pending: HashMap::new(),
            bus: HashMap::new(),
            sessions: HashMap::new(),
            init: InitSlot::None,
            init_waiters: HashMap::new(),
            degraded: false,
            degraded_reason: None,
            backoff_ms,
            stable_timer: None,
            hook_events: 0,
            mcp_events: 0,
            rate_limited: false,
            stopped: false,
        }
    }

    pub fn session(&self, key: &str) -> Option<u64> {
        self.sessions.get(key).copied()
    }

    pub fn set_session(&mut self, key: &str, sid: u64) {
        self.sessions.insert(key.to_string(), sid);
    }

    pub fn bus_get(&self, tid: Option<&ThreadId>) -> Option<u64> {
        self.bus.get(tid?).copied()
    }

    pub fn bus_set(&mut self, tid: ThreadId, sid: u64) {
        self.bus.insert(tid, sid);
    }

    fn next_rpc_id(&mut self) -> u64 {
        let id = self.rpc_id;
        self.rpc_id += 1;
        id
    }

    fn kill_child(&self) {
        if let Some(pid) = self.pid {
            // SAFETY: kill(2) on the pid of a child this process spawned.
            unsafe {
                libc::kill(pid, libc::SIGTERM);
            }
        }
    }
}

fn log(w: &mut World, kind: &str, obj: Value) {
    w.log(kind, obj);
}

/// `ensure()`: the degraded gate, then the one in-flight boot. `Ok(None)` = already up.
pub fn ensure(
    w: &mut World,
    pi: usize,
) -> Result<Option<oneshot::Receiver<InitResult>>, SidecarError> {
    let p = &w.personas[pi];
    if p.degraded {
        let why = p
            .degraded_reason
            .as_ref()
            .map_or("null".to_string(), as_text);
        return Err(err(
            503,
            "api_error",
            format!(
                "persona {} is degraded after an out-of-band execution attempt ({why}); its codex app-server is being restarted",
                p.name
            ),
        ));
    }
    if p.init == InitSlot::None {
        set_init(w, pi, None);
    }
    let p = &mut w.personas[pi];
    match p.init {
        InitSlot::Ready(_) => Ok(None),
        InitSlot::Pending(gen) => {
            let (tx, rx) = oneshot::channel();
            p.init_waiters.entry(gen).or_default().push(tx);
            Ok(Some(rx))
        }
        InitSlot::None => Err(err(502, "api_error", "persona boot failed")),
    }
}

/// Starts the boot (now, or after the restart path's delay) and records it as pending.
fn set_init(w: &mut World, pi: usize, delay_ms: Option<u64>) {
    let gen = w.id();
    w.personas[pi].init = InitSlot::Pending(gen);
    let me = w.shared();
    tokio::spawn(async move {
        if let Some(d) = delay_ms {
            tokio::time::sleep(std::time::Duration::from_millis(d)).await;
            let stopped = with_world(&me, |w| {
                if w.personas[pi].stopped {
                    let name = w.personas[pi].name.clone();
                    complete(
                        w,
                        pi,
                        gen,
                        Err(err(
                            503,
                            "api_error",
                            format!("persona {name} is shutting down"),
                        )),
                    );
                    true
                } else {
                    false
                }
            });
            if stopped != Some(false) {
                return;
            }
        }
        let Some(rx) = with_world(&me, |w| {
            spawn_child(w, pi);
            rpc(
                w,
                pi,
                "initialize",
                json!({ "clientInfo": { "name": "codex-sidecar", "version": VERSION }, "capabilities": { "experimentalApi": true } }),
                0,
            )
        }) else {
            return;
        };
        let r = rx.wait().await;
        with_world(&me, |w| match r {
            Ok(r) => {
                send(
                    w,
                    pi,
                    &json!({ "jsonrpc": "2.0", "method": "initialized", "params": {} }),
                );
                let p = &mut w.personas[pi];
                p.degraded = false;
                p.degraded_reason = None;
                let name = p.name.clone();
                let ua = r
                    .get("userAgent")
                    .filter(|v| !v.is_null())
                    .cloned()
                    .unwrap_or(Value::Null);
                log(
                    w,
                    "initialized",
                    json!({ "persona": name, "userAgent": ua }),
                );
                complete(w, pi, gen, Ok(()));
            }
            Err(e) => complete(w, pi, gen, Err(e)),
        });
    });
}

fn complete(w: &mut World, pi: usize, gen: u64, r: InitResult) {
    let p = &mut w.personas[pi];
    if p.init == InitSlot::Pending(gen) {
        p.init = if r.is_ok() {
            InitSlot::Ready(gen)
        } else {
            InitSlot::None
        };
    }
    for tx in p.init_waiters.remove(&gen).unwrap_or_default() {
        let _ = tx.send(r.clone());
    }
}

fn spawn_child(w: &mut World, pi: usize) {
    let cmd = w.cfg.app_server_cmd.clone();
    let name = w.personas[pi].name.clone();
    let (file, args): (String, Vec<String>) = if !cmd.is_empty() {
        let s = cmd.replace("{persona}", &name);
        let parts: Vec<String> = s
            .split(char::is_whitespace)
            .filter(|x| !x.is_empty())
            .map(String::from)
            .collect();
        let file = parts.first().cloned().unwrap_or_default();
        let mut args: Vec<String> = parts.iter().skip(1).cloned().collect();
        args.push("app-server".into());
        (file, args)
    } else {
        (name.clone(), vec!["app-server".into()])
    };
    let mut c = tokio::process::Command::new(&file);
    c.args(&args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (k, v) in &w.cfg.env {
        c.env(k, v);
    }
    c.env("SIDECAR_PERSONA", &name);
    c.env_remove("SIDECAR_KEY");
    let me = w.shared();
    let gen = w.id();
    let spawned = c.spawn();
    {
        let p = &mut w.personas[pi];
        p.child_gen = gen;
        p.child_alive = true;
        p.stdin = None;
        p.pid = None;
    }
    match spawned {
        Ok(mut child) => {
            w.personas[pi].pid = child.id().map(|x| x as i32);
            let (tx, mut rx) = mpsc::unbounded_channel::<String>();
            w.personas[pi].stdin = Some(tx);
            if let Some(mut stdin) = child.stdin.take() {
                let me2 = me.clone();
                let nm = name.clone();
                tokio::spawn(async move {
                    while let Some(line) = rx.recv().await {
                        if let Err(e) = stdin.write_all(line.as_bytes()).await {
                            let msg = format!("write: {e}");
                            with_world(&me2, |w| {
                                log(w, "child-stdin-error", json!({ "persona": nm, "err": msg }))
                            });
                            break;
                        }
                    }
                });
            }
            if let Some(mut stderr) = child.stderr.take() {
                let me2 = me.clone();
                let nm = name.clone();
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 65536];
                    while let Ok(n) = stderr.read(&mut buf).await {
                        if n == 0 {
                            break;
                        }
                        let text = String::from_utf8_lossy(&buf[..n]).to_string();
                        let text: String = text.chars().take(2000).collect();
                        with_world(&me2, |w| {
                            log(w, "child-stderr", json!({ "persona": nm, "text": text }))
                        });
                    }
                });
            }
            let stdout = child.stdout.take();
            let me2 = me.clone();
            tokio::spawn(async move {
                if let Some(out) = stdout {
                    let mut out = BufReader::new(out);
                    let mut line: Vec<u8> = Vec::new();
                    // one frame per newline; a trailing partial line at EOF is dropped
                    while let Ok(n) = out.read_until(b'\n', &mut line).await {
                        if n == 0 || line.last() != Some(&b'\n') {
                            break;
                        }
                        let l = String::from_utf8_lossy(&line[..line.len() - 1]).into_owned();
                        line.clear();
                        let done = with_world(&me2, |w| {
                            if w.personas[pi].child_gen == gen {
                                on_line(w, pi, &l);
                            }
                        });
                        if done.is_none() {
                            return;
                        }
                    }
                }
                let st = child.wait().await;
                use std::os::unix::process::ExitStatusExt;
                let (code, sig, e) = match st {
                    Ok(s) => (
                        s.code().map_or(Value::Null, Value::from),
                        s.signal()
                            .map_or(Value::Null, |x| Value::from(signal_name(x))),
                        Value::Null,
                    ),
                    Err(e) => (Value::Null, Value::Null, Value::from(e.to_string())),
                };
                with_world(&me2, |w| on_exit(w, pi, gen, code, sig, e));
            });
        }
        Err(e) => {
            let msg = format!("spawn {file}: {e}");
            let me2 = me.clone();
            tokio::spawn(async move {
                with_world(&me2, |w| {
                    on_exit(w, pi, gen, Value::Null, Value::Null, Value::from(msg))
                });
            });
        }
    }
    log(
        w,
        "child-started",
        json!({ "persona": name, "cmd": file, "args": args }),
    );
    let stable = tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(60000)).await;
        with_world(&me, |w| {
            let b = w.cfg.backoff_ms;
            let p = &mut w.personas[pi];
            p.stable_timer = None;
            p.backoff_ms = b;
        });
    });
    if let Some(old) = w.personas[pi].stable_timer.replace(stable.abort_handle()) {
        old.abort();
    }
}

fn on_exit(w: &mut World, pi: usize, gen: u64, code: Value, signal: Value, e: Value) {
    let p = &mut w.personas[pi];
    if !p.child_alive || p.child_gen != gen {
        return;
    }
    p.child_alive = false;
    if let Some(t) = p.stable_timer.take() {
        t.abort();
    }
    let name = p.name.clone();
    let msg = format!(
        "the codex app-server for {name} exited (code={} signal={})",
        as_text(&code),
        as_text(&signal)
    );
    log(
        w,
        "child-exit",
        json!({ "persona": name, "code": code, "signal": signal, "err": e }),
    );
    let ex = err(502, "api_error", msg);
    let p = &mut w.personas[pi];
    for (_, tx) in p.pending.drain() {
        let _ = tx.send(Err(ex.clone()));
    }
    let bus: Vec<u64> = p.bus.values().copied().collect();
    for sid in bus {
        if let Some(s) = w.sessions.get_mut(&sid) {
            s.parked.clear();
        }
        session::on_frame(w, sid, Frame::ChildExit(ex.clone()));
        session::close(w, sid, "app-server exited");
    }
    let p = &mut w.personas[pi];
    p.bus.clear();
    p.sessions.clear();
    p.init = InitSlot::None;
    p.stdin = None;
    p.pid = None;
    if p.stopped {
        return;
    }
    let wait = p.backoff_ms;
    p.backoff_ms = p.backoff_ms.saturating_mul(2).min(30_000);
    set_init(w, pi, Some(wait));
}

fn on_line(w: &mut World, pi: usize, line: &str) {
    if line.trim().is_empty() {
        return;
    }
    match serde_json::from_str::<Value>(line) {
        Ok(m) => on_frame(w, pi, m),
        Err(_) => {
            let name = w.personas[pi].name.clone();
            let l: String = line.chars().take(400).collect();
            log(w, "unparsed-frame", json!({ "persona": name, "line": l }));
        }
    }
}

fn rpc_key(v: &Value) -> Option<u64> {
    let f = v.as_f64()?;
    (f >= 0.0 && f.fract() == 0.0).then_some(f as u64)
}

fn on_frame(w: &mut World, pi: usize, m: Value) {
    if !m.is_object() {
        if m.is_null() {
            let name = w.personas[pi].name.clone();
            log(
                w,
                "frame-error",
                json!({ "persona": name, "err": "frame is null, expected a JSON object" }),
            );
        }
        return;
    }
    // Every field is optional and `Value`-or-absent, so an object always parses.
    let f: RpcFrame = serde_json::from_value(m).unwrap_or_default();
    // response to one of our requests
    if f.id.is_some() && f.method.is_none() {
        let Some(tx) =
            f.id.as_ref()
                .and_then(rpc_key)
                .and_then(|k| w.personas[pi].pending.remove(&k))
        else {
            return;
        };
        let r = match &f.error {
            Some(e) if is_set(Some(e)) => Err(map_upstream_error(e, "app-server")),
            _ => Ok(f.result.unwrap_or(Value::Null)),
        };
        let _ = tx.send(r);
        return;
    }
    let tid = f.thread_id();
    // server -> client request
    if is_set(f.method.as_ref()) && f.id.is_some() {
        let id = f.id.clone().unwrap_or(Value::Null);
        if f.method_str() == Some("item/tool/call") {
            if let Some(sid) = w.personas[pi].bus_get(tid.as_ref()) {
                session::on_frame(w, sid, Frame::ToolCall(ToolCall::of(&f)));
                return;
            }
            send(
                w,
                pi,
                &json!({ "jsonrpc": "2.0", "id": id, "result": { "contentItems": [{ "type": "inputText", "text": "no active session for this thread" }], "success": false } }),
            );
            return;
        }
        let method = f.method.clone().unwrap_or(Value::Null);
        if as_text(&method).to_ascii_lowercase().contains("approval") {
            send(
                w,
                pi,
                &json!({ "jsonrpc": "2.0", "id": id, "result": { "decision": "denied" } }),
            );
            let sid = w.personas[pi].bus_get(tid.as_ref());
            trip_boundary(w, pi, method, sid);
            return;
        }
        send(w, pi, &json!({ "jsonrpc": "2.0", "id": id, "result": {} }));
        return;
    }
    // notification
    if !is_set(f.method.as_ref()) {
        return;
    }
    let Some(method) = f.method_str() else {
        return;
    };
    let p = &mut w.personas[pi];
    if method == codex::HOOK_STARTED {
        p.hook_events += 1;
    }
    if method == codex::MCP_STARTUP {
        p.mcp_events += 1;
    }
    if method == "account/rateLimits/updated" {
        let limits = codex::rate_limits(f.params.as_ref());
        let hit = rate_limit_exhaustion(limits.as_ref(), crate::logging::now_ms());
        let Some(hit) = hit else {
            return;
        };
        let reason = as_text(&hit.reason);
        let retry_after = hit.retry_after.map(|r| r as u64);
        let mut e = err(
            429,
            "rate_limit_error",
            format!("codex account rate limit reached ({reason})"),
        );
        e.retry_after = retry_after;
        let name = p.name.clone();
        let ra = retry_after.map_or(Value::Null, Value::from);
        log(
            w,
            "rate-limited",
            json!({ "persona": name, "reason": hit.reason, "retryAfter": ra }),
        );
        w.personas[pi].rate_limited = true;
        let bus: Vec<u64> = w.personas[pi].bus.values().copied().collect();
        for sid in &bus {
            let Some(ok) = w.sessions.get(sid).and_then(|s| s.outbox_key.clone()) else {
                continue;
            };
            if w.cfg.outbox_path.is_empty() {
                continue;
            }
            crate::outbox_events::rate_limit_exhausted(
                &mut w.outbox,
                &name,
                &ok,
                &hit.reason,
                retry_after,
            );
        }
        for sid in bus {
            session::settle(w, sid, Outcome::Error(e.clone()));
        }
        return;
    }
    if let Some(sid) = w.personas[pi].bus_get(tid.as_ref()) {
        let note = Note::parse(method, f.params.as_ref());
        session::on_frame(w, sid, Frame::Note(note));
    }
}

/// `send(obj)`: one newline-framed frame on the child's stdin.
pub fn send(w: &mut World, pi: usize, obj: &Value) -> bool {
    let p = &w.personas[pi];
    if !p.child_alive {
        return false;
    }
    match &p.stdin {
        Some(tx) => tx.send(format!("{obj}\n")).is_ok(),
        None => true,
    }
}

/// An issued JSON-RPC call; `wait` is its answer under the call's timeout.
pub struct Call {
    rx: oneshot::Receiver<RpcResult>,
    me: Weak<Mutex<World>>,
    pi: usize,
    id: u64,
    method: String,
    timeout_ms: u64,
}

impl Call {
    pub async fn wait(mut self) -> RpcResult {
        let dropped = || Err(err(502, "api_error", "rpc dropped"));
        if self.timeout_ms == 0 {
            return self.rx.await.unwrap_or_else(|_| dropped());
        }
        let t = Duration::from_millis(self.timeout_ms);
        match tokio::time::timeout(t, &mut self.rx).await {
            Ok(r) => r.unwrap_or_else(|_| dropped()),
            Err(_) => {
                let (pi, id) = (self.pi, self.id);
                with_world(&self.me, |w| {
                    w.personas[pi].pending.remove(&id);
                    // an answer that landed in the same instant wins
                    self.rx.try_recv().ok()
                })
                .flatten()
                .unwrap_or_else(|| {
                    Err(err(
                        502,
                        "api_error",
                        format!(
                            "timed out waiting for the codex app-server ({})",
                            self.method
                        ),
                    ))
                })
            }
        }
    }
}

/// `rpc(method, params, timeoutMs)`; `timeout_ms == 0` waits without a limit.
pub fn rpc(w: &mut World, pi: usize, method: &str, params: Value, timeout_ms: u64) -> Call {
    let (tx, rx) = oneshot::channel();
    let id = w.personas[pi].next_rpc_id();
    w.personas[pi].pending.insert(id, tx);
    let frame = json!({ "jsonrpc": "2.0", "id": id, "method": method, "params": params });
    if !send(w, pi, &frame) {
        if let Some(tx) = w.personas[pi].pending.remove(&id) {
            let name = w.personas[pi].name.clone();
            let _ = tx.send(Err(err(
                502,
                "api_error",
                format!("the codex app-server for {name} is not running"),
            )));
        }
    }
    Call {
        rx,
        me: w.shared(),
        pi,
        id,
        method: method.to_string(),
        timeout_ms,
    }
}

/// `turn/interrupt` for a session's open turn; `Some(threadId)` when one was sent.
fn send_interrupt(w: &mut World, pi: usize, sid: u64) -> Option<ThreadId> {
    let (t, u) = w
        .sessions
        .get(&sid)
        .and_then(|s| Some((s.thread_id.clone()?, s.turn_id.clone()?)))?;
    let id = w.personas[pi].next_rpc_id();
    send(
        w,
        pi,
        &json!({ "jsonrpc": "2.0", "id": id, "method": "turn/interrupt", "params": { "threadId": t, "turnId": u } }),
    );
    Some(t)
}

/// The security boundary: interrupt, fail closed, degrade, restart.
pub fn trip_boundary(w: &mut World, pi: usize, what: Value, sid: Option<u64>) {
    let tid = sid
        .and_then(|s| w.sessions.get(&s))
        .and_then(|s| s.thread_id.clone());
    let name = w.personas[pi].name.clone();
    log(
        w,
        "security-event",
        json!({ "persona": name, "itemType": what, "threadId": tid, "action": "turn/interrupt + degrade + restart" }),
    );
    let ws = as_text(&what);
    let p = &mut w.personas[pi];
    p.degraded = true;
    p.degraded_reason = Some(what);
    let e = err(
        502,
        "api_error",
        format!("out-of-band execution attempt ({ws}) from the codex app-server; the turn was interrupted and persona {name} marked degraded"),
    );
    let bus: Vec<u64> = p.bus.values().copied().collect();
    for sid in bus {
        send_interrupt(w, pi, sid);
        session::settle(w, sid, Outcome::Error(e.clone()));
    }
    let me = w.shared();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        with_world(&me, |w| w.personas[pi].kill_child());
    });
}

pub fn interrupt(w: &mut World, pi: usize, sid: u64, reason: &str) {
    if let Some(t) = send_interrupt(w, pi, sid) {
        let name = w.personas[pi].name.clone();
        log(
            w,
            "turn-interrupt",
            json!({ "persona": name, "threadId": t, "reason": reason }),
        );
    }
}

/// Closes the session and unmaps its key — only while the key still maps to it, so dropping
/// a displaced session never unmaps its successor.
pub fn drop_session(w: &mut World, pi: usize, sid: u64) {
    session::close(w, sid, "session dropped");
    if let Some(key) = w.sessions.get(&sid).map(|s| s.key.clone()) {
        let p = &mut w.personas[pi];
        if p.session(&key) == Some(sid) {
            p.sessions.remove(&key);
        }
    }
}

pub fn stop(w: &mut World, pi: usize) {
    let p = &mut w.personas[pi];
    p.stopped = true;
    if let Some(t) = p.stable_timer.take() {
        t.abort();
    }
    let ss: Vec<u64> = p.sessions.values().copied().collect();
    for sid in ss {
        session::close(w, sid, "sidecar shutting down");
    }
    w.personas[pi].kill_child();
}

/// A wire value as log/message text: a string as-is, anything else as its JSON.
fn as_text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}
