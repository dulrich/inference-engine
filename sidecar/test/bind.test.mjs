// bind.test.mjs — the docker0-gateway bind race (plan sidecar-bind-and-router-health-detection WP-1).
// The extra bind can fail at boot (docker0 absent → EADDRNOTAVAIL); the sidecar must retry
// it and must never report `ok` while a bind is missing. We reproduce "fails, then succeeds"
// with EADDRINUSE on 127.0.0.2 — a real bind failure without touching docker0.
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { withSidecar, req, waitFor } from "./helpers.mjs";

const EXTRA = "127.0.0.2";

function occupy(host) {
  return new Promise((resolve, reject) => {
    const s = http.createServer((_r, res) => res.end());
    s.once("error", reject);
    s.listen(0, host, () => resolve({ port: s.address().port, release: () => new Promise(r => s.close(r)) }));
  });
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
  const h = await withSidecar({ env: {
    SIDECAR_PORT: String(blocker.port), SIDECAR_BIND_EXTRA: EXTRA,
    SIDECAR_BIND_RETRY_MS: "50", SIDECAR_BIND_RETRY_BUDGET_MS: "5000",
  } });
  t.after(async () => { await h.close(); await blocker.release().catch(() => {}); });

  const before = await req(h, "/healthz", { method: "GET" });
  assert.equal(before.status, 503, before.text);
  assert.equal(before.json.status, "degraded");
  assert.deepEqual(before.json.binds.missing, [`${EXTRA}:${blocker.port}`]);
  assert.ok(h.log().some(l => l.kind === "bind-extra-retrying" && /EADDRINUSE/.test(l.err)));

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
  const h = await withSidecar({ env: {
    SIDECAR_PORT: String(blocker.port), SIDECAR_BIND_EXTRA: EXTRA,
    SIDECAR_BIND_RETRY_MS: "20", SIDECAR_BIND_RETRY_BUDGET_MS: "200",
  } });
  t.after(async () => { await h.close(); await blocker.release(); });

  await waitFor(() => h.log().some(l => l.kind === "bind-extra-failed"), 3000);
  const gaveUp = h.log().find(l => l.kind === "bind-extra-failed");
  assert.ok(gaveUp.attempts >= 2 && gaveUp.elapsedMs <= 1000, JSON.stringify(gaveUp));
  assert.ok(!h.log().some(l => l.kind === "bind-extra-recovered"));

  const r = await req(h, "/healthz", { method: "GET" });
  assert.equal(r.status, 503, r.text);
  assert.equal(r.json.status, "degraded");
  assert.deepEqual(r.json.binds.missing, [`${EXTRA}:${blocker.port}`]);
});
