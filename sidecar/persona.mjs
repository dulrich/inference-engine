// persona.mjs — one `<persona> app-server` child, and everything spoken over its stdio.
//
// A Persona owns the process: spawn, the newline-framed JSON-RPC codec, the pending-request
// table, the thread bus that routes a frame to its Session, the sessions map, account
// rate-limit posture, the security boundary (`tripBoundary`), and the backoff restart.
//
// Two invariants worth naming. One in-flight boot per persona, so a request arriving
// mid-backoff waits for the scheduled restart instead of spawning a second child. And the
// approval policy is "never", so an approval REQUEST is itself evidence codex tried to
// execute: it is denied, the boundary is tripped, and the persona degrades and restarts.
import { spawn } from "node:child_process";
import { SidecarError, mapUpstreamError, rateLimitExhaustion } from "./errors.mjs";
import { VERSION } from "./config.mjs";

export class Persona {
  constructor(name, ctx) {
    this.name = name;
    this.ctx = ctx;
    this.child = null;
    this.childAlive = false;
    this.buf = "";
    this.rpcId = 1;
    this.pending = new Map();     // rpc id -> {resolve, reject}
    this.bus = new Map();         // threadId -> Session
    this.sessions = new Map();    // session key -> Session
    this.initPromise = null;
    this.degraded = false;
    this.degradedReason = null;
    this.backoffMs = ctx.config.backoffMs;
    this.restartTimer = null;
    this.stableTimer = null;
    this.hookEvents = 0;
    this.mcpEvents = 0;
    this.rateLimit = null;
    this.rateLimited = false;     // an exhaustion row is outstanding; the next turn clears it
    this.stopped = false;
  }

  log(kind, obj) { this.ctx.logger.write(kind, obj); }

  get alive() { return this.childAlive; }

  async ensure() {
    if (this.degraded) {
      throw new SidecarError(503, "api_error",
        `persona ${this.name} is degraded after an out-of-band execution attempt (${this.degradedReason}); its codex app-server is being restarted`);
    }
    if (!this.initPromise) this.setInit(this.boot());
    return this.initPromise;
  }

  // One in-flight boot per persona: a request arriving mid-backoff waits for the
  // scheduled restart instead of spawning a second child.
  setInit(p) {
    this.initPromise = p;
    p.catch(() => { if (this.initPromise === p) this.initPromise = null; });
    return p;
  }

  boot() {
    return (async () => {
      this.spawnChild();
      const r = await this.rpc("initialize", {
        clientInfo: { name: "codex-sidecar", version: VERSION },
        capabilities: { experimentalApi: true },
      });
      this.send({ jsonrpc: "2.0", method: "initialized", params: {} });
      this.degraded = false;
      this.degradedReason = null;
      this.log("initialized", { persona: this.name, userAgent: r?.userAgent ?? null });
      return r;
    })();
  }

  spawnChild() {
    const cmd = this.ctx.config.appServerCmd;
    let file, args;
    if (cmd) {
      const parts = cmd.replace(/\{persona\}/g, this.name).split(/\s+/).filter(Boolean);
      file = parts[0];
      args = [...parts.slice(1), "app-server"];
    } else {
      file = this.name;            // the wrapper on PATH; never a hard-coded codex path
      args = ["app-server"];
    }
    const env = { ...process.env, ...this.ctx.config.env, SIDECAR_PERSONA: this.name };
    delete env.SIDECAR_KEY;        // the child never needs the bridge key
    const child = spawn(file, args, { stdio: ["pipe", "pipe", "pipe"], env });
    this.child = child;
    this.childAlive = true;
    this.buf = "";
    // a write that races the child's death must never become an uncaught EPIPE
    child.stdin.on("error", e => this.log("child-stdin-error", { persona: this.name, err: String(e?.message || e) }));
    child.stdout.on("error", () => { /* closed with the child */ });
    child.stderr.on("error", () => { /* closed with the child */ });
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", d => this.onData(d));
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", d => this.log("child-stderr", { persona: this.name, text: String(d).slice(0, 2000) }));
    child.on("error", e => this.onExit(null, null, String(e?.message || e)));
    child.on("exit", (code, signal) => this.onExit(code, signal, null));
    this.log("child-started", { persona: this.name, cmd: file, args });
    if (this.stableTimer) clearTimeout(this.stableTimer);
    this.stableTimer = setTimeout(() => { this.backoffMs = this.ctx.config.backoffMs; }, 60000);
    this.stableTimer.unref?.();
  }

  onExit(code, signal, err) {
    if (!this.childAlive) return;
    this.childAlive = false;
    if (this.stableTimer) { clearTimeout(this.stableTimer); this.stableTimer = null; }
    this.log("child-exit", { persona: this.name, code, signal, err });
    const e = new SidecarError(502, "api_error",
      `the codex app-server for ${this.name} exited (code=${code} signal=${signal})`);
    for (const [, p] of this.pending) p.reject(e);
    this.pending.clear();
    for (const s of [...this.bus.values()]) { s.parked.clear(); s.onFrame({ type: "child-exit", error: e }); s.close("app-server exited"); }
    this.bus.clear();
    this.sessions.clear();
    this.initPromise = null;
    this.child = null;
    this.buf = "";
    if (this.stopped) return;
    const wait = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, 30000);
    // bypasses the degraded gate on purpose: a completed restart is what clears `degraded`
    this.setInit((async () => {
      await new Promise(r => { this.restartTimer = setTimeout(r, wait); this.restartTimer.unref?.(); });
      if (this.stopped) throw new SidecarError(503, "api_error", `persona ${this.name} is shutting down`);
      return this.boot();
    })());
  }

  onData(chunk) {
    this.buf += chunk;
    let i;
    while ((i = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, i);
      this.buf = this.buf.slice(i + 1);
      if (!line.trim()) continue;
      let m;
      try { m = JSON.parse(line); } catch { this.log("unparsed-frame", { persona: this.name, line: line.slice(0, 400) }); continue; }
      try { this.onFrame(m); } catch (e) { this.log("frame-error", { persona: this.name, err: String(e?.stack || e) }); }
    }
  }

  onFrame(m) {
    // response to one of our requests
    if (m.id !== undefined && m.method === undefined) {
      const p = this.pending.get(m.id);
      if (!p) return;
      this.pending.delete(m.id);
      if (m.error) p.reject(mapUpstreamError(m.error, "app-server"));
      else p.resolve(m.result);
      return;
    }
    // server -> client request
    if (m.method && m.id !== undefined) {
      if (m.method === "item/tool/call") {
        const s = this.bus.get(m.params?.threadId);
        if (s) { s.onFrame({ type: "toolcall", rpc: m }); return; }
        this.send({ jsonrpc: "2.0", id: m.id, result: { contentItems: [{ type: "inputText", text: "no active session for this thread" }], success: false } });
        return;
      }
      if (/approval/i.test(m.method)) {
        // approvalPolicy is "never": an approval request means codex tried to execute.
        this.send({ jsonrpc: "2.0", id: m.id, result: { decision: "denied" } });
        const s = this.bus.get(m.params?.threadId);
        this.tripBoundary(m.method, s || null);
        return;
      }
      this.send({ jsonrpc: "2.0", id: m.id, result: {} });
      return;
    }
    // notification
    if (!m.method) return;
    if (m.method === "hook/started") this.hookEvents += 1;
    if (m.method === "mcpServer/startupStatus/updated") this.mcpEvents += 1;
    if (m.method === "account/rateLimits/updated") {
      const hit = rateLimitExhaustion(m.params?.rateLimits);
      this.rateLimit = hit;
      if (hit) {
        const err = new SidecarError(429, "rate_limit_error", `codex account rate limit reached (${hit.reason})`);
        if (hit.retryAfter != null) err.extra.retryAfter = hit.retryAfter;
        this.log("rate-limited", { persona: this.name, reason: hit.reason, retryAfter: hit.retryAfter ?? null });
        // One outbox row per session settled by the exhaustion; the flag makes the next
        // successful turn on this persona publish the matching `{state:'ok'}`.
        this.rateLimited = true;
        for (const s of [...this.bus.values()]) {
          if (!s.outboxKey || !this.ctx.config.outboxPath) continue;
          this.ctx.outbox.record({
            harness: "codex-sidecar", leg: this.name,
            session_id: s.outboxKey.session_id, agent_id: s.outboxKey.agent_id, agent_type: null,
            kind: "rate_limit",
            payload: { state: "exhausted", reason: hit.reason, retry_after_s: hit.retryAfter ?? undefined },
          });
        }
        for (const s of [...this.bus.values()]) s.settle({ kind: "error", error: err });
      }
      return;
    }
    const s = this.bus.get(m.params?.threadId);
    if (s) s.onFrame({ type: "note", frame: m });
  }

  send(obj) {
    if (!this.childAlive || !this.child?.stdin?.writable) return false;
    try { this.child.stdin.write(`${JSON.stringify(obj)}\n`); } catch { return false; }
    return true;
  }

  // `timeoutMs > 0` bounds the await (Defect E2): an app-server that never answers
  // `thread/start`/`turn/start` used to park the HTTP handler forever, because the only
  // guard (`Session.waitOutcome`) is armed after this await returns. The rejection is the
  // same 502 the outcome timeout raises, so it surfaces the ordinary `http-error` row.
  rpc(method, params, timeoutMs = 0) {
    return new Promise((resolve, reject) => {
      const id = this.rpcId++;
      let timer = null;
      // settling always drops the pending entry, so a late response is ignored, never a crash
      const once = fn => v => {
        if (timer) { clearTimeout(timer); timer = null; }
        this.pending.delete(id);
        fn(v);
      };
      const entry = { resolve: once(resolve), reject: once(reject) };
      this.pending.set(id, entry);
      if (timeoutMs > 0) {
        timer = setTimeout(() => {
          entry.reject(new SidecarError(502, "api_error",
            `timed out waiting for the codex app-server (${method})`));
        }, timeoutMs);
        timer.unref?.();
      }
      if (!this.send({ jsonrpc: "2.0", id, method, params })) {
        entry.reject(new SidecarError(502, "api_error", `the codex app-server for ${this.name} is not running`));
      }
    });
  }

  // The security boundary: an out-of-band item type (or an approval request) means
  // codex has its own execution surface live. Interrupt, fail closed, degrade, restart.
  tripBoundary(what, session) {
    this.log("security-event", {
      persona: this.name, itemType: what, threadId: session?.threadId ?? null,
      action: "turn/interrupt + degrade + restart",
    });
    this.degraded = true;
    this.degradedReason = what;
    const err = new SidecarError(502, "api_error",
      `out-of-band execution attempt (${what}) from the codex app-server; the turn was interrupted and persona ${this.name} marked degraded`);
    for (const s of [...this.bus.values()]) {
      if (s.threadId && s.turnId) this.send({ jsonrpc: "2.0", id: this.rpcId++, method: "turn/interrupt", params: { threadId: s.threadId, turnId: s.turnId } });
      s.settle({ kind: "error", error: err });
    }
    // an interrupted child does not exit on its own; kill it so the restart path runs
    setTimeout(() => { try { this.child?.kill("SIGTERM"); } catch { /* ignore */ } }, 50).unref?.();
  }

  interrupt(session, reason) {
    if (session?.threadId && session?.turnId) {
      this.send({ jsonrpc: "2.0", id: this.rpcId++, method: "turn/interrupt", params: { threadId: session.threadId, turnId: session.turnId } });
      this.log("turn-interrupt", { persona: this.name, threadId: session.threadId, reason });
    }
  }

  dropSession(session) {
    if (!session) return;
    session.close("session dropped");
    this.sessions.delete(session.key);
  }

  stop() {
    this.stopped = true;
    if (this.restartTimer) clearTimeout(this.restartTimer);
    if (this.stableTimer) clearTimeout(this.stableTimer);
    for (const s of [...this.sessions.values()]) s.close("sidecar shutting down");
    try { this.child?.kill("SIGTERM"); } catch { /* ignore */ }
  }
}
