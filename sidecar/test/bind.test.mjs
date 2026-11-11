// bind.test.mjs — the docker0-gateway bind race (plan sidecar-bind-and-router-health-detection WP-1).
// The extra bind can fail at boot (docker0 absent → EADDRNOTAVAIL); the sidecar must retry
// it and must never report `ok` while a bind is missing. We reproduce "fails, then succeeds"
// with EADDRINUSE on 127.0.0.2 — a real bind failure without touching docker0.
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { spawn } from "node:child_process";
import { withSidecar, req, waitFor } from "./helpers.mjs";

const EXTRA = "127.0.0.2";

// Hold 127.0.0.1:P bound with SO_REUSEADDR, never listening (test-ports.md pattern 2/3a): the
// sidecar's primary bind (tokio TcpListener::bind, SO_REUSEADDR on Unix) shares it. The perl
// child prints P and exits when its stdin closes, so it can never outlive the suite.
const HOLD = `use Socket qw(:all); socket(my $r, PF_INET, SOCK_STREAM, 0) or die; setsockopt($r, SOL_SOCKET, SO_REUSEADDR, 1) or die;
bind($r, pack_sockaddr_in(0, inet_aton("127.0.0.1"))) or die; $|=1; print +(sockaddr_in(getsockname($r)))[0], "\\n"; 1 while <STDIN>;`;

function reserve() {
  return new Promise((resolve, reject) => {
    const c = spawn("perl", ["-e", HOLD], { stdio: ["pipe", "pipe", "inherit"] });
    let out = "";
    c.once("error", reject);
    c.once("exit", code => reject(new Error(`reserve helper exited ${code}`)));
    c.stdout.on("data", d => {
      out += d;
      if (out.includes("\n")) resolve({ port: Number(out), release: () => { c.stdin.end(); c.kill(); } });
    });
  });
}

// Whole footprint held before the sidecar starts: 127.0.0.1:P by the shareable reservation,
// host:P by a listener that makes the extra bind fail until released.
async function occupy(host) {
  for (let i = 0; ; i++) {
    const dead = await reserve();
    try {
      const s = http.createServer((_r, res) => res.end());
      await new Promise((res, rej) => { s.once("error", rej); s.listen(dead.port, host, res); });
      return { port: dead.port, releaseReservation: dead.release,
        release: () => { dead.release(); return new Promise(r => s.close(r)); } };
    } catch (e) { dead.release(); if (i >= 4) throw e; }
  }
}

test("both binds present: /healthz is ok and lists them", async t => {
  const h = await withSidecar({ env: { SIDECAR_BIND_EXTRA: EXTRA } });
  t.after(() => h.close());
  const r = await req(h, "/healthz", { method: "GET" });
  assert.equal(r.status, 200, r.text);
  assert.equal(r.json.status, "ok");
  assert.deepEqual(r.json.binds.expected, [`127.0.0.1:${h.port}`, `${EXTRA}:${h.port}`]);
  assert.deepEqual(r.json.binds.bound, r.json.binds.expected);
  assert.deepEqual(r.json.binds.missing, []);
  const listen = h.log().find(l => l.kind === "listen");
  assert.deepEqual(listen.binds, r.json.binds.bound);
  assert.ok(!h.log().some(l => l.kind.startsWith("bind-extra")), "no bind-extra lines on the happy path");
});

test("extra bind fails then succeeds on retry: degraded until recovered, then ok", async t => {
  const blocker = await occupy(EXTRA);
  // Release the held port even if the target fails to start, or the run hangs instead of failing.
  let h;
  t.after(async () => { await h?.close(); await blocker.release().catch(() => {}); });
  h = await withSidecar({ env: {
    SIDECAR_PORT: String(blocker.port), SIDECAR_BIND_EXTRA: EXTRA,
    SIDECAR_BIND_RETRY_MS: "50", SIDECAR_BIND_RETRY_BUDGET_MS: "5000",
  } });
  blocker.releaseReservation(); // primary is serving; the EXTRA listener still blocks

  const before = await req(h, "/healthz", { method: "GET" });
  assert.equal(before.status, 503, before.text);
  assert.equal(before.json.status, "degraded");
  assert.deepEqual(before.json.binds.missing, [`${EXTRA}:${blocker.port}`]);
  assert.ok(h.log().some(l => l.kind === "bind-extra-retrying" && /[Aa]ddress already in use|AddrInUse/.test(l.err)));

  await blocker.release();
  await waitFor(() => h.log().some(l => l.kind === "bind-extra-recovered"), 4000);
  const rec = h.log().find(l => l.kind === "bind-extra-recovered");
  assert.ok(rec.attempts >= 2, `attempts=${rec.attempts}`);

  const after = await req(h, "/healthz", { method: "GET" });
  assert.equal(after.status, 200, after.text);
  assert.equal(after.json.status, "ok");
  assert.deepEqual(after.json.binds.missing, []);
  assert.ok(!h.log().some(l => l.kind === "bind-extra-failed"));

  // and the recovered bind actually serves
  const viaExtra = await new Promise((resolve, reject) => {
    http.get({ host: EXTRA, port: blocker.port, path: "/healthz", headers: { authorization: "Bearer test-key" } },
      res => { let b = ""; res.on("data", d => (b += d)); res.on("end", () => resolve({ status: res.statusCode, body: b })); })
      .on("error", reject);
  });
  assert.equal(viaExtra.status, 200, viaExtra.body);
});

test("extra bind never succeeds: gives up within budget, stays degraded", async t => {
  const blocker = await occupy(EXTRA);
  // Release the held port even if the target fails to start, or the run hangs instead of failing.
  let h;
  t.after(async () => { await h?.close(); await blocker.release().catch(() => {}); });
  h = await withSidecar({ env: {
    SIDECAR_PORT: String(blocker.port), SIDECAR_BIND_EXTRA: EXTRA,
    SIDECAR_BIND_RETRY_MS: "20", SIDECAR_BIND_RETRY_BUDGET_MS: "200",
  } });
  blocker.releaseReservation(); // primary is serving; the EXTRA listener still blocks

  await waitFor(() => h.log().some(l => l.kind === "bind-extra-failed"), 3000);
  const gaveUp = h.log().find(l => l.kind === "bind-extra-failed");
  assert.ok(gaveUp.attempts >= 2 && gaveUp.elapsedMs <= 1000, JSON.stringify(gaveUp));
  assert.ok(!h.log().some(l => l.kind === "bind-extra-recovered"));

  const r = await req(h, "/healthz", { method: "GET" });
  assert.equal(r.status, 503, r.text);
  assert.equal(r.json.status, "degraded");
  assert.deepEqual(r.json.binds.missing, [`${EXTRA}:${blocker.port}`]);
});
