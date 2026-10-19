// install.sh under the fleet-env cutover switch: stubbed fleet-env, systemctl and persona
// wrappers on PATH, a mkdtemp --home. Never touches the real home, units or store.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

const INSTALL = path.join(import.meta.dirname, "..", "install.sh");
const DUMMY = "fixture-sidecar-key-not-a-secret";

function setup({ on, projection = true, args = [], prepare } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "sidecar-install-"));
  const bin = path.join(root, "bin");
  const home = path.join(root, "home");
  fs.mkdirSync(bin);
  fs.mkdirSync(home);
  const calls = path.join(root, "calls");
  const stub = (name, body) => {
    fs.writeFileSync(path.join(bin, name), `#!/usr/bin/env bash\n${body}\n`);
    fs.chmodSync(path.join(bin, name), 0o755);
  };
  for (const p of ["codex-a", "codex-b"]) stub(p, "exit 0");
  stub("systemctl", `echo "systemctl $*" >> "${calls}"`);
  stub("loginctl", `echo "loginctl $*" >> "${calls}"`);
  const proj = path.join(root, "xdg", "fleet-env", "codex-sidecar.env");
  if (projection) {
    fs.mkdirSync(path.dirname(proj), { recursive: true });
    fs.writeFileSync(proj, "", { mode: 0o600 });
  }
  stub("fleet-env", `echo "fleet-env $1 $2" >> "${calls}"
case "$1" in
  enabled) exit ${on ? 0 : 1} ;;
  path) echo "${proj}" ;;
  run) shift 3; CODEX_SIDECAR_KEY=${DUMMY} exec "$@" ;;
  *) exit 2 ;;
esac`);
  if (prepare) prepare({ root, home });
  const run = () => spawnSync("bash", [INSTALL, "--home", home, ...args], {
    encoding: "utf8",
    env: { PATH: `${bin}:${path.dirname(process.execPath)}:/usr/bin:/bin`, HOME: root, FLEET_ENV_BIN: path.join(bin, "fleet-env") },
  });
  const r = run();
  const unit = path.join(home, ".config", "systemd", "user", "codex-sidecar.service");
  return {
    ...r,
    root,
    home,
    run,
    unit: fs.existsSync(unit) ? fs.readFileSync(unit, "utf8") : "",
    legacyEnv: path.join(home, ".config", "codex-sidecar", "env"),
    calls: fs.existsSync(calls) ? fs.readFileSync(calls, "utf8") : "",
  };
}

test("switch off: legacy EnvironmentFile, no fleet-env ordering, legacy env file generated", () => {
  const r = setup({ on: false });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.unit, /^EnvironmentFile=%h\/\.config\/codex-sidecar\/env$/m);
  assert.doesNotMatch(r.unit, /fleet-env|@FLEET_ENV_UNIT@|@ENV_FILE@/);
  assert.ok(fs.existsSync(r.legacyEnv));
  assert.equal(r.calls, "fleet-env enabled \n");
});

test("switch on: projection EnvironmentFile + After/Requires, no legacy file, no key printed", () => {
  const r = setup({ on: true });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.unit, /^EnvironmentFile=%t\/fleet-env\/codex-sidecar\.env$/m);
  assert.match(r.unit, /^After=fleet-env-project\.service$/m);
  assert.match(r.unit, /^Requires=fleet-env-project\.service$/m);
  assert.ok(!fs.existsSync(r.legacyEnv), "no legacy env file under the cutover");
  assert.ok(!fs.existsSync(path.dirname(r.legacyEnv)), "legacy config dir untouched");
  assert.ok(!(r.stdout + r.stderr).includes(DUMMY));
  assert.doesNotMatch(r.stdout, /Generated SIDECAR_KEY/);
  assert.match(r.calls, /fleet-env run sidecar-install/);
  assert.doesNotMatch(r.calls, /systemctl/, "test home never reloads systemd");
});

test("switch on with the projection missing fails closed", () => {
  const r = setup({ on: true, projection: false });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /is missing; run fleet-env project/);
  assert.ok(!fs.existsSync(r.legacyEnv));
});

// --impl rust: a stub "binary" stands in for the release build (no cargo in tests).
function rustStub(root, tag) {
  const p = path.join(root, `stub-${tag}`);
  fs.writeFileSync(p, `#!/bin/sh\n# ${tag}\nexit 0\n`);
  fs.chmodSync(p, 0o755);
  return p;
}

test("node default render: ExecStart is <node> <sidecar.mjs>, node dir on PATH", () => {
  const r = setup({ on: false });
  assert.equal(r.status, 0, r.stderr);
  const sidecar = path.join(path.dirname(INSTALL), "codex-sidecar.mjs");
  assert.match(r.unit, new RegExp(`^ExecStart=/\\S+/node ${sidecar.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`, "m"));
  assert.match(r.unit, new RegExp(`^Environment=PATH=${path.dirname(process.execPath).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}:`, "m"));
  assert.doesNotMatch(r.unit, /@EXEC@|@NODE@|@SIDECAR@|@PATH@/);
  assert.ok(!fs.existsSync(path.join(r.home, ".local", "lib", "codex-sidecar")), "node impl installs no binary");
});

test("rust render: ExecStart is the installed binary, no node path, binary 0755", () => {
  let stub;
  const r = setup({ on: false, prepare: ({ root }) => { stub = rustStub(root, "v1"); }, args: [] });
  // first run above was the node default; re-run as rust against the same home
  const rr = spawnSync("bash", [INSTALL, "--home", r.home, "--impl", "rust", "--rust-bin", stub], {
    encoding: "utf8",
    env: { PATH: `${path.join(r.root, "bin")}:/usr/bin:/bin`, HOME: r.root, FLEET_ENV_BIN: path.join(r.root, "bin", "fleet-env") },
  });
  assert.equal(rr.status, 0, rr.stderr);
  const unit = fs.readFileSync(path.join(r.home, ".config", "systemd", "user", "codex-sidecar.service"), "utf8");
  const dest = path.join(r.home, ".local", "lib", "codex-sidecar", "codex-sidecar");
  assert.match(unit, new RegExp(`^ExecStart=${dest.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`, "m"));
  assert.match(unit, new RegExp(`^Environment=PATH=${path.join(r.root, "bin").replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}:/usr/local/bin:/usr/bin:/bin$`, "m"));
  assert.doesNotMatch(unit, /\.nvm|node|@EXEC@/);
  assert.equal(fs.statSync(dest).mode & 0o777, 0o755);
  assert.match(fs.readFileSync(dest, "utf8"), /# v1/);
  assert.doesNotMatch(rr.stdout, /cargo build/);
});

test("rust reinstall keeps the previous build as codex-sidecar.prev", () => {
  let v1, v2;
  const r = setup({ on: false, args: ["--impl", "rust", "--rust-bin", "PLACEHOLDER"], prepare: ({ root }) => { v1 = rustStub(root, "v1"); v2 = rustStub(root, "v2"); } });
  assert.notEqual(r.status, 0, "placeholder path is rejected");
  assert.match(r.stderr, /--rust-bin is not an executable file/);
  const env = { PATH: `${path.join(r.root, "bin")}:/usr/bin:/bin`, HOME: r.root, FLEET_ENV_BIN: path.join(r.root, "bin", "fleet-env") };
  for (const b of [v1, v2]) {
    const x = spawnSync("bash", [INSTALL, "--home", r.home, "--impl", "rust", "--rust-bin", b], { encoding: "utf8", env });
    assert.equal(x.status, 0, x.stderr);
  }
  const lib = path.join(r.home, ".local", "lib", "codex-sidecar");
  assert.match(fs.readFileSync(path.join(lib, "codex-sidecar"), "utf8"), /# v2/);
  assert.match(fs.readFileSync(path.join(lib, "codex-sidecar.prev"), "utf8"), /# v1/);
  assert.deepEqual(fs.readdirSync(lib).sort(), ["codex-sidecar", "codex-sidecar.prev"], "no temp files left behind");
});
