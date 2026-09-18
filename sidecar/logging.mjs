// logging.mjs — the durable JSONL request log.
//
// One day-rolled file per process under a 0700 directory, each file 0600, pruned past the
// retention window. `write` is deliberately total: logging must never break a request, so
// every failure here is swallowed. `redactHeaders` is the one rule about what may be written.
import fs from "node:fs";
import path from "node:path";

export class Logger {
  constructor(dir, keepDays) {
    this.dir = dir;
    this.keepDays = keepDays;
    this.day = null;
    this.fd = null;
    this.file = null;
  }
  static dayOf(d = new Date()) { return d.toISOString().slice(0, 10); }
  rollover() {
    const day = Logger.dayOf();
    if (this.fd !== null && day === this.day) return;
    if (this.fd !== null) { try { fs.closeSync(this.fd); } catch { /* ignore */ } this.fd = null; }
    fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
    try { fs.chmodSync(this.dir, 0o700); } catch { /* ignore */ }
    const file = path.join(this.dir, `codex-sidecar-${day}.jsonl`);
    this.fd = fs.openSync(file, "a", 0o600);
    try { fs.chmodSync(file, 0o600); } catch { /* ignore */ }
    this.day = day;
    this.file = file;
    this.prune();
  }
  prune(now = Date.now()) {
    const cutoff = now - this.keepDays * 86400000;
    let names = [];
    try { names = fs.readdirSync(this.dir); } catch { return; }
    for (const n of names) {
      const m = /^codex-sidecar-(\d{4}-\d{2}-\d{2})\.jsonl$/.exec(n);
      if (!m || n === path.basename(this.file || "")) continue;
      const t = Date.parse(`${m[1]}T00:00:00Z`);
      if (Number.isFinite(t) && t < cutoff) { try { fs.unlinkSync(path.join(this.dir, n)); } catch { /* ignore */ } }
    }
  }
  write(kind, obj = {}) {
    try {
      this.rollover();
      fs.writeSync(this.fd, `${JSON.stringify({ ts: new Date().toISOString(), kind, ...obj })}\n`);
    } catch { /* logging must never break a request */ }
  }
  close() { if (this.fd !== null) { try { fs.closeSync(this.fd); } catch { /* ignore */ } this.fd = null; } }
}

export function redactHeaders(headers = {}) {
  const out = {};
  for (const [k, v] of Object.entries(headers)) {
    out[k] = /^(authorization|x-api-key|cookie|proxy-authorization)$/i.test(k) ? "<redacted>" : v;
  }
  return out;
}
