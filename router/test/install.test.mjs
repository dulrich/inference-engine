// Tests for router/install.sh in test-home mode (--home other than $HOME): files only, never
// systemctl, loginctl or curl. The node config, env file and binary are fixtures this test
// builds; no committed config or catalog content is asserted.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const INSTALL = path.join(import.meta.dirname, "..", "install.sh");

function fixture(t, { providers, envExtra = "" } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "router-install-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const cfg = path.join(dir, "node.gateway.config.json");
  fs.writeFileSync(cfg, JSON.stringify({ host: "0.0.0.0", port: 4813, providers: providers ?? [
    { name: "local", type: "openai_chat_completions", apikey: "x", baseurl: "http://inference-engine-llama:4806/v1" },
    { name: "hosted", type: "anthropic", baseurl: "https://api.example.com" },
    { name: "codex-a", type: "anthropic", apiKeyEnv: "K", baseurl: "http://host.docker.internal:4877/codex-a" },
    { name: "loop", type: "anthropic", baseurl: "http://127.0.0.1:9999" },
  ] }));
  const env = path.join(dir, "inference-router.env");
  fs.writeFileSync(env, `INFERENCE_ROUTER_GATEWAY_KEY=gk\nINFERENCE_ROUTER_CONFIG_FILE=${cfg}\n${envExtra}`);
  const bin = path.join(dir, "stub-router");
  fs.writeFileSync(bin, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  const home = path.join(dir, "home");
  return { dir, env, bin, home };
}

function run(args) {
  // HOME stays the caller's: --home differs from it, so the installer runs in test mode.
  const r = spawnSync(INSTALL, args, { encoding: "utf8", env: { ...process.env, PATH: "/usr/bin:/bin" } });
  return { code: r.status, out: r.stdout, err: r.stderr };
}

const lib = home => path.join(home, ".local/lib/inference-router");
const units = home => path.join(home, ".config/systemd/user");

test("render rewrites exactly the two container upstreams and writes the generation", t => {
  const f = fixture(t);
  const r = run(["--home", f.home, "--env-file", f.env, "--rust-bin", f.bin]);
  assert.equal(r.code, 0, r.err);
  const cur = fs.readFileSync(path.join(lib(f.home), "current"), "utf8").trim();
  assert.match(cur, /^\d{8}T\d{6}Z-[0-9a-f]{8}$/);
  const cfg = JSON.parse(fs.readFileSync(path.join(lib(f.home), "generations", cur, "gateway.config.json"), "utf8"));
  assert.deepEqual(cfg.providers.map(p => p.baseurl), [
    "http://127.0.0.1:4811/v1", "https://api.example.com", "http://127.0.0.1:4877/codex-a", "http://127.0.0.1:9999",
  ]);
  const unit = fs.readFileSync(path.join(units(f.home), "inference-router@.service"), "utf8");
  assert.match(unit, /^Sockets=inference-router\.socket$/m);
  assert.match(unit, new RegExp(`^EnvironmentFile=${f.env}$`, "m"));
  assert.doesNotMatch(unit, /@[A-Z_]+@/);
  assert.ok(fs.existsSync(path.join(units(f.home), "inference-router.socket")));
  assert.ok(!fs.existsSync(path.join(units(f.home), "inference-router-wg.socket")));
  assert.match(r.out, /systemd skipped/);
});

test("a container-only upstream other than the two rewrites is an install error", t => {
  const f = fixture(t, { providers: [{ name: "x", type: "anthropic", baseurl: "http://some-compose-service:8080/v1" }] });
  const r = run(["--home", f.home, "--env-file", f.env, "--rust-bin", f.bin]);
  assert.equal(r.code, 1);
  assert.match(r.err, /container-only upstream some-compose-service:8080/);
  assert.ok(!fs.existsSync(path.join(lib(f.home), "current")));
});

test("the WireGuard socket is opt-in and refuses a wildcard address", t => {
  const f = fixture(t, { envExtra: "INFERENCE_ROUTER_WG_BIND_ADDRESS=192.0.2.1\nWG_BIND_ADDRESS=198.51.100.9\n" });
  const r = run(["--home", f.home, "--env-file", f.env, "--rust-bin", f.bin]);
  assert.equal(r.code, 0, r.err);
  const sock = fs.readFileSync(path.join(units(f.home), "inference-router-wg.socket"), "utf8");
  assert.match(sock, /^ListenStream=10\.8\.0\.1:4813$/m);
  assert.match(sock, /^FreeBind=yes$/m);
  assert.match(fs.readFileSync(path.join(units(f.home), "inference-router@.service"), "utf8"),
    /^Sockets=inference-router\.socket inference-router-wg\.socket$/m);
  for (const wild of ["0.0.0.0", "::", "*", ""]) {
    const g = fixture(t, { envExtra: `INFERENCE_ROUTER_WG_BIND_ADDRESS=${wild}\n` });
    const w = run(["--home", g.home, "--env-file", g.env, "--rust-bin", g.bin]);
    assert.equal(w.code, 1, `wildcard ${JSON.stringify(wild)} refused`);
    assert.match(w.err, /wildcard or empty address/);
  }
});

test("--preflight prints the topology and writes nothing; a missing env file is an error", t => {
  const f = fixture(t);
  const r = run(["--preflight", "--home", f.home, "--env-file", f.env]);
  assert.equal(r.code, 0, r.err);
  assert.match(r.out, /^listen {2}127\.0\.0\.1:4813$/m);
  assert.match(r.out, /^upstream local http:\/\/127\.0\.0\.1:4811\/v1$/m);
  assert.ok(!fs.existsSync(f.home), "preflight wrote nothing");
  const m = run(["--preflight", "--home", f.home, "--env-file", path.join(f.dir, "absent.env")]);
  assert.equal(m.code, 1);
  assert.match(m.err, /env file is missing/);
});

test("generations: current/previous pointers, prune keeps two, --rollback reuses the previous directory", t => {
  const f = fixture(t);
  const ids = [];
  for (let i = 0; i < 3; i++) {
    assert.equal(run(["--home", f.home, "--env-file", f.env, "--rust-bin", f.bin]).code, 0);
    ids.push(fs.readFileSync(path.join(lib(f.home), "current"), "utf8").trim());
  }
  assert.equal(new Set(ids).size, 3, "a fresh activation id per run");
  assert.deepEqual(fs.readdirSync(path.join(lib(f.home), "generations")).sort(), ids.slice(1).sort());
  assert.equal(fs.readFileSync(path.join(lib(f.home), "previous"), "utf8").trim(), ids[1]);
  const prevCfg = fs.readFileSync(path.join(lib(f.home), "generations", ids[1], "gateway.config.json"), "utf8");
  const rb = run(["--rollback", "--home", f.home, "--env-file", f.env]);
  assert.equal(rb.code, 0, rb.err);
  assert.match(rb.out, new RegExp(`rollback to generation ${ids[1]}`));
  const now = fs.readFileSync(path.join(lib(f.home), "current"), "utf8").trim();
  assert.ok(!ids.includes(now), "rollback is a new generation");
  assert.equal(
    fs.readFileSync(path.join(lib(f.home), "generations", now, "gateway.config.json"), "utf8"),
    prevCfg);
  assert.equal(fs.readFileSync(path.join(lib(f.home), "previous"), "utf8").trim(), ids[2]);
});
