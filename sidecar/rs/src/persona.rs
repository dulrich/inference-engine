//! `persona.mjs` — one `<persona> app-server` child and everything spoken over its stdio:
//! spawn, the newline-framed JSON-RPC codec, the pending-request table, the thread bus, the
//! sessions map, rate-limit posture, the security boundary (`trip_boundary`) and the backoff
//! restart. One reader task per child; every frame is handled under the world lock.

use crate::config::VERSION;
use crate::errors::{map_upstream_error, rate_limit_exhaustion, SidecarError};
use crate::js;
use crate::session::{self, same_value, Frame, Outcome};
use crate::sidecar::{err, with_world, World};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::process::Stdio;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::{mpsc, oneshot};

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
    /// threadId -> session id (a JS `Map`, insertion order).
    pub bus: Vec<(Value, u64)>,
    /// session key -> session id (a JS `Map`, insertion order).
    pub sessions: Vec<(String, u64)>,
    init: InitSlot,
    init_waiters: HashMap<u64, Vec<oneshot::Sender<InitResult>>>,
    pub degraded: bool,
    degraded_reason: Option<Value>,
    backoff_ms: u64,
    stable_gen: u64,
    pub hook_events: u64,
    pub mcp_events: u64,
    pub rate_limited: bool,
    stopped: bool,
}

extern "C" {
    fn kill(pid: i32, sig: i32) -> i32;
}

fn signal_name(sig: i32) -> String {
    let n = match sig {
        1 => "SIGHUP",
        2 => "SIGINT",
        3 => "SIGQUIT",
        4 => "SIGILL",
        5 => "SIGTRAP",
        6 => "SIGABRT",
        7 => "SIGBUS",
        8 => "SIGFPE",
        9 => "SIGKILL",
        10 => "SIGUSR1",
        11 => "SIGSEGV",
        12 => "SIGUSR2",
        13 => "SIGPIPE",
        14 => "SIGALRM",
        15 => "SIGTERM",
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
            bus: Vec::new(),
            sessions: Vec::new(),
            init: InitSlot::None,
            init_waiters: HashMap::new(),
            degraded: false,
            degraded_reason: None,
            backoff_ms,
            stable_gen: 0,
            hook_events: 0,
            mcp_events: 0,
            rate_limited: false,
            stopped: false,
        }
    }

    pub fn session(&self, key: &str) -> Option<u64> {
        self.sessions
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, s)| *s)
    }

    pub fn set_session(&mut self, key: &str, sid: u64) {
        match self.sessions.iter_mut().find(|(k, _)| k == key) {
            Some(e) => e.1 = sid,
            None => self.sessions.push((key.to_string(), sid)),
        }
    }

    pub fn bus_get(&self, tid: Option<&Value>) -> Option<u64> {
        let tid = tid?;
        self.bus
            .iter()
            .find(|(k, _)| same_value(k, tid))
            .map(|(_, s)| *s)
    }

    pub fn bus_set(&mut self, tid: Value, sid: u64) {
        match self.bus.iter_mut().find(|(k, _)| same_value(k, &tid)) {
            Some(e) => e.1 = sid,
            None => self.bus.push((tid, sid)),
        }
    }

    fn next_rpc_id(&mut self) -> u64 {
        let id = self.rpc_id;
        self.rpc_id += 1;
        id
    }

    fn kill_child(&self) {
        if let Some(pid) = self.pid {
            // SAFETY: libc kill(2) on the pid of a child this process spawned.
            unsafe {
                kill(pid, 15);
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
            .map_or("null".to_string(), js::js_string);
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

/// `setInit(boot())`, or the restart path's delayed boot.
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
        let r = rx
            .await
            .unwrap_or_else(|_| Err(err(502, "api_error", "rpc dropped")));
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
                let ua = js::present(js::prop(Some(&r), "userAgent"))
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
            .split(js::is_js_space)
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
                            let msg = if e.kind() == std::io::ErrorKind::BrokenPipe {
                                "write EPIPE".to_string()
                            } else {
                                e.to_string()
                            };
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
                        let text = js::utf16_prefix(&text, 2000).to_string();
                        with_world(&me2, |w| {
                            log(w, "child-stderr", json!({ "persona": nm, "text": text }))
                        });
                    }
                });
            }
            let stdout = child.stdout.take();
            let me2 = me.clone();
            tokio::spawn(async move {
                if let Some(mut out) = stdout {
                    let mut buf: Vec<u8> = Vec::new();
                    let mut chunk = vec![0u8; 65536];
                    while let Ok(n) = out.read(&mut chunk).await {
                        if n == 0 {
                            break;
                        }
                        buf.extend_from_slice(&chunk[..n]);
                        let mut lines = Vec::new();
                        while let Some(i) = buf.iter().position(|b| *b == b'\n') {
                            let line: Vec<u8> = buf.drain(..=i).collect();
                            lines.push(String::from_utf8_lossy(&line[..i]).to_string());
                        }
                        if !lines.is_empty() {
                            let done = with_world(&me2, |w| {
                                for l in lines {
                                    if w.personas[pi].child_gen == gen {
                                        on_line(w, pi, &l);
                                    }
                                }
                            });
                            if done.is_none() {
                                return;
                            }
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
            let msg = if e.kind() == std::io::ErrorKind::NotFound {
                format!("spawn {file} ENOENT")
            } else if e.kind() == std::io::ErrorKind::PermissionDenied {
                format!("spawn {file} EACCES")
            } else {
                format!("spawn {file} {e}")
            };
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
    let p = &mut w.personas[pi];
    p.stable_gen += 1;
    let sg = p.stable_gen;
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(60000)).await;
        with_world(&me, |w| {
            if w.personas[pi].stable_gen == sg {
                w.personas[pi].backoff_ms = w.cfg.backoff_ms;
            }
        });
    });
}

fn on_exit(w: &mut World, pi: usize, gen: u64, code: Value, signal: Value, e: Value) {
    let p = &mut w.personas[pi];
    if !p.child_alive || p.child_gen != gen {
        return;
    }
    p.child_alive = false;
    p.stable_gen += 1;
    let name = p.name.clone();
    let msg = format!(
        "the codex app-server for {name} exited (code={} signal={})",
        js::js_string(&code),
        js::js_string(&signal)
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
    let bus: Vec<u64> = p.bus.iter().map(|(_, s)| *s).collect();
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
    if js::trim(line).is_empty() {
        return;
    }
    match serde_json::from_str::<Value>(line) {
        Ok(m) => on_frame(w, pi, m),
        Err(_) => {
            let name = w.personas[pi].name.clone();
            let l = js::utf16_prefix(line, 400).to_string();
            log(w, "unparsed-frame", json!({ "persona": name, "line": l }));
        }
    }
}

fn rpc_key(v: &Value) -> Option<u64> {
    let f = v.as_f64()?;
    (f >= 0.0 && f.fract() == 0.0).then_some(f as u64)
}

fn on_frame(w: &mut World, pi: usize, m: Value) {
    let Value::Object(ref o) = m else {
        if m.is_null() {
            let name = w.personas[pi].name.clone();
            log(
                w,
                "frame-error",
                json!({ "persona": name, "err": "frame is null, expected a JSON object" }),
            );
        }
        return;
    };
    let id = o.get("id");
    let method = o.get("method");
    // response to one of our requests
    if id.is_some() && method.is_none() {
        let Some(tx) = id
            .and_then(rpc_key)
            .and_then(|k| w.personas[pi].pending.remove(&k))
        else {
            return;
        };
        let r = match o.get("error") {
            Some(e) if js::truthy(Some(e)) => Err(map_upstream_error(e, "app-server")),
            _ => Ok(o.get("result").cloned().unwrap_or(Value::Null)),
        };
        let _ = tx.send(r);
        return;
    }
    let method_s = method.map(js::js_string).unwrap_or_default();
    let params = o.get("params");
    let tid = js::prop(params, "threadId");
    // server -> client request
    if js::truthy(method) && id.is_some() {
        let id = id.cloned().unwrap_or(Value::Null);
        if method == Some(&Value::from("item/tool/call")) {
            if let Some(sid) = w.personas[pi].bus_get(tid) {
                session::on_frame(w, sid, Frame::ToolCall(m.clone()));
                return;
            }
            send(
                w,
                pi,
                &json!({ "jsonrpc": "2.0", "id": id, "result": { "contentItems": [{ "type": "inputText", "text": "no active session for this thread" }], "success": false } }),
            );
            return;
        }
        if method_s.to_ascii_lowercase().contains("approval") {
            send(
                w,
                pi,
                &json!({ "jsonrpc": "2.0", "id": id, "result": { "decision": "denied" } }),
            );
            let sid = w.personas[pi].bus_get(tid);
            trip_boundary(w, pi, method.cloned().unwrap_or(Value::Null), sid);
            return;
        }
        send(w, pi, &json!({ "jsonrpc": "2.0", "id": id, "result": {} }));
        return;
    }
    // notification
    if !js::truthy(method) {
        return;
    }
    let p = &mut w.personas[pi];
    if method_s == "hook/started" && method.is_some_and(Value::is_string) {
        p.hook_events += 1;
    }
    if method_s == "mcpServer/startupStatus/updated" && method.is_some_and(Value::is_string) {
        p.mcp_events += 1;
    }
    if method == Some(&Value::from("account/rateLimits/updated")) {
        let hit = rate_limit_exhaustion(js::prop(params, "rateLimits"), js::now_ms());
        let Some(hit) = hit else {
            return;
        };
        let reason = js::js_string(&hit.reason);
        let mut e = err(
            429,
            "rate_limit_error",
            format!("codex account rate limit reached ({reason})"),
        );
        if let Some(ra) = hit.retry_after {
            let mut x = Map::new();
            x.insert("retryAfter".into(), js::num(ra));
            e = e.with_extra(x);
        }
        let name = p.name.clone();
        let ra = hit.retry_after.map_or(Value::Null, js::num);
        log(
            w,
            "rate-limited",
            json!({ "persona": name, "reason": hit.reason, "retryAfter": ra }),
        );
        w.personas[pi].rate_limited = true;
        let bus: Vec<u64> = w.personas[pi].bus.iter().map(|(_, s)| *s).collect();
        for sid in &bus {
            let Some(ok) = w.sessions.get(sid).and_then(|s| s.outbox_key.clone()) else {
                continue;
            };
            if w.cfg.outbox_path.is_empty() {
                continue;
            }
            let mut payload = Map::new();
            payload.insert("state".into(), Value::from("exhausted"));
            payload.insert("reason".into(), hit.reason.clone());
            if let Some(r) = hit.retry_after {
                payload.insert("retry_after_s".into(), js::num(r));
            }
            let row = json!({
                "harness": "codex-sidecar", "leg": name,
                "session_id": ok.session_id, "agent_id": ok.agent_id, "agent_type": null,
                "kind": "rate_limit", "payload": payload,
            });
            w.outbox.record(&row);
        }
        for sid in bus {
            session::settle(w, sid, Outcome::Error(e.clone()));
        }
        return;
    }
    if let Some(sid) = w.personas[pi].bus_get(tid) {
        session::on_frame(w, sid, Frame::Note(m.clone()));
    }
}

/// `send(obj)`: one newline-framed frame on the child's stdin.
pub fn send(w: &mut World, pi: usize, obj: &Value) -> bool {
    let p = &w.personas[pi];
    if !p.child_alive {
        return false;
    }
    match &p.stdin {
        Some(tx) => tx.send(format!("{}\n", js::stringify(obj))).is_ok(),
        None => true,
    }
}

/// `rpc(method, params, timeoutMs)`.
pub fn rpc(
    w: &mut World,
    pi: usize,
    method: &str,
    params: Value,
    timeout_ms: u64,
) -> oneshot::Receiver<RpcResult> {
    let (tx, rx) = oneshot::channel();
    let id = w.personas[pi].next_rpc_id();
    w.personas[pi].pending.insert(id, tx);
    if timeout_ms > 0 {
        let me = w.shared();
        let m = method.to_string();
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(timeout_ms)).await;
            with_world(&me, |w| {
                if let Some(tx) = w.personas[pi].pending.remove(&id) {
                    let _ = tx.send(Err(err(
                        502,
                        "api_error",
                        format!("timed out waiting for the codex app-server ({m})"),
                    )));
                }
            });
        });
    }
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
    rx
}

/// The security boundary: interrupt, fail closed, degrade, restart.
pub fn trip_boundary(w: &mut World, pi: usize, what: Value, sid: Option<u64>) {
    let tid = sid
        .and_then(|s| w.sessions.get(&s))
        .and_then(|s| s.thread_id.clone())
        .unwrap_or(Value::Null);
    let name = w.personas[pi].name.clone();
    log(
        w,
        "security-event",
        json!({ "persona": name, "itemType": what, "threadId": tid, "action": "turn/interrupt + degrade + restart" }),
    );
    let ws = js::js_string(&what);
    let p = &mut w.personas[pi];
    p.degraded = true;
    p.degraded_reason = Some(what);
    let e = err(
        502,
        "api_error",
        format!("out-of-band execution attempt ({ws}) from the codex app-server; the turn was interrupted and persona {name} marked degraded"),
    );
    let bus: Vec<u64> = p.bus.iter().map(|(_, s)| *s).collect();
    for sid in bus {
        let ids = w
            .sessions
            .get(&sid)
            .and_then(|s| match (&s.thread_id, &s.turn_id) {
                (Some(t), Some(u)) if js::truthy(Some(t)) && js::truthy(Some(u)) => {
                    Some((t.clone(), u.clone()))
                }
                _ => None,
            });
        if let Some((t, u)) = ids {
            let id = w.personas[pi].next_rpc_id();
            send(
                w,
                pi,
                &json!({ "jsonrpc": "2.0", "id": id, "method": "turn/interrupt", "params": { "threadId": t, "turnId": u } }),
            );
        }
        session::settle(w, sid, Outcome::Error(e.clone()));
    }
    let me = w.shared();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        with_world(&me, |w| w.personas[pi].kill_child());
    });
}

pub fn interrupt(w: &mut World, pi: usize, sid: u64, reason: &str) {
    let ids = w
        .sessions
        .get(&sid)
        .and_then(|s| match (&s.thread_id, &s.turn_id) {
            (Some(t), Some(u)) if js::truthy(Some(t)) && js::truthy(Some(u)) => {
                Some((t.clone(), u.clone()))
            }
            _ => None,
        });
    if let Some((t, u)) = ids {
        let id = w.personas[pi].next_rpc_id();
        send(
            w,
            pi,
            &json!({ "jsonrpc": "2.0", "id": id, "method": "turn/interrupt", "params": { "threadId": t, "turnId": u } }),
        );
        let name = w.personas[pi].name.clone();
        log(
            w,
            "turn-interrupt",
            json!({ "persona": name, "threadId": t, "reason": reason }),
        );
    }
}

pub fn drop_session(w: &mut World, pi: usize, sid: u64) {
    session::close(w, sid, "session dropped");
    if let Some(key) = w.sessions.get(&sid).map(|s| s.key.clone()) {
        w.personas[pi].sessions.retain(|(k, _)| *k != key);
    }
}

pub fn stop(w: &mut World, pi: usize) {
    let p = &mut w.personas[pi];
    p.stopped = true;
    p.stable_gen += 1;
    let ss: Vec<u64> = p.sessions.iter().map(|(_, s)| *s).collect();
    for sid in ss {
        session::close(w, sid, "sidecar shutting down");
    }
    w.personas[pi].kill_child();
}
