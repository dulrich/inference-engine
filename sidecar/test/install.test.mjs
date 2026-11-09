// install.sh, store-only (no switch, no legacy env file): stubbed fleet-env, systemctl and persona
// wrappers on PATH, a mkdtemp --home. Never touches the real home, units or store.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

const INSTALL = path.join(import.meta.dirname, "..", "install.sh");
const DUMMY = "fixture-sidecar-key-not-a-secret";

function setup({ projection = true, args = [], prepare, fleetEnvBin, noPersonas = false } = {}) {
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
  for (const p of ["codex", "codex-a", "codex-b"]) stub(p, "exit 0");
  stub("systemctl", `echo "systemctl $*" >> "${calls}"`);
  stub("loginctl", `echo "loginctl $*" >> "${calls}"`);
  const proj = path.join(root, "xdg", "fleet-env", "codex-sidecar.env");
  if (projection) {
    fs.mkdirSync(path.dirname(proj), { recursive: true });
    fs.writeFileSync(proj, "", { mode: 0o600 });
  }
  stub("fleet-env", `echo "fleet-env $1 $2" >> "${calls}"
case "$1" in
  path) echo "${proj}" ;;
  run) shift 3; CODEX_SIDECAR_KEY=${DUMMY} exec "$@" ;;
  *) exit 2 ;;
esac`);
  if (prepare) prepare({ root, home });
  // A stub "binary" stands in for the release build (no cargo in tests) unless args pass one.
  const binArgs = args.includes("--rust-bin") ? [] : ["--rust-bin", rustStub(root, "v0")];
  const personaArgs = args.some((a) => a.startsWith("--personas")) || noPersonas ? [] : ["--personas", "codex-a,codex-b"];
  const run = () => spawnSync("bash", [INSTALL, "--home", home, ...binArgs, ...personaArgs, ...args], {
    encoding: "utf8",
    env: { PATH: `${bin}:${path.dirname(process.execPath)}:/usr/bin:/bin`, HOME: root, FLEET_ENV_BIN: fleetEnvBin ?? path.join(bin, "fleet-env") },
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

test("store-only: projection EnvironmentFile + After/Requires, no legacy file, no key printed", () => {
  const r = setup();
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.unit, /^EnvironmentFile=%t\/fleet-env\/codex-sidecar\.env$/m);
  assert.match(r.unit, /^After=fleet-env-project\.service$/m);
  assert.match(r.unit, /^Requires=fleet-env-project\.service$/m);
  assert.ok(!fs.existsSync(r.legacyEnv), "no legacy env file is created");
  assert.ok(!fs.existsSync(path.dirname(r.legacyEnv)), "legacy config dir untouched");
  assert.ok(!(r.stdout + r.stderr).includes(DUMMY));
  assert.doesNotMatch(r.stdout, /Generated SIDECAR_KEY/);
  assert.match(r.calls, /fleet-env run sidecar-install/);
  assert.doesNotMatch(r.calls, /systemctl/, "test home never reloads systemd");
});

test("store-only: the projection missing fails closed", () => {
  const r = setup({ projection: false });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /is missing; run fleet-env project/);
  assert.ok(!fs.existsSync(r.legacyEnv));
});

test("store-only: a missing fleet-env binary fails closed, nothing written", () => {
  const r = setup({ fleetEnvBin: "/nonexistent/fleet-env" });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /not found on PATH/);
  assert.equal(r.unit, "", "no unit written");
  assert.ok(!fs.existsSync(path.dirname(r.legacyEnv)), "no legacy config dir");
});

test("personas: default is stock codex; ReadWritePaths and SIDECAR_PERSONAS render from the list", () => {
  const d = setup({ noPersonas: true });
  assert.equal(d.status, 0, d.stderr);
  assert.match(d.unit, /^ReadWritePaths=%h\/\.codex %h\/\.local\/state\/codex-sidecar$/m);
  assert.match(d.unit, /^Environment=SIDECAR_PERSONAS=codex$/m);
  const two = setup({ args: ["--personas", "codex-a,codex-b"] });
  assert.equal(two.status, 0, two.stderr);
  assert.match(two.unit, /^ReadWritePaths=%h\/\.codex-a %h\/\.codex-b %h\/\.local\/state\/codex-sidecar$/m);
  assert.match(two.unit, /^Environment=SIDECAR_PERSONAS=codex-a,codex-b$/m);
  assert.doesNotMatch(two.unit, /@[A-Z_]+@/);
});

test("personas: invalid, duplicate and missing-wrapper personas fail loudly", () => {
  for (const bad of ["bob", "codex-", "codex-a,codex-a", "codex-a/../x", " , "]) {
    const r = setup({ args: ["--personas", bad] });
    assert.notEqual(r.status, 0, bad);
    assert.equal(r.unit, "", `no unit for ${bad}`);
  }
  const r = setup({ args: ["--personas", "codex-nowrapper"] });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /persona wrapper 'codex-nowrapper' not found on PATH/);
});

test("--env-file: mode 0600 enforced, EnvironmentFile points at it, no fleet-env use or ordering", () => {
  let good, loose, nokey;
  const prep = ({ root }) => {
    good = path.join(root, "good.env"); fs.writeFileSync(good, "SIDECAR_KEY=" + DUMMY + "\n", { mode: 0o600 });
    loose = path.join(root, "loose.env"); fs.writeFileSync(loose, "SIDECAR_KEY=" + DUMMY + "\n"); fs.chmodSync(loose, 0o644);
    nokey = path.join(root, "nokey.env"); fs.writeFileSync(nokey, "OTHER=1\n", { mode: 0o600 });
  };
  const ok = setup({ prepare: prep, fleetEnvBin: "/nonexistent/fleet-env", args: [] });
  // prepare ran before the first run, so re-run the real case with the prepared path
  const run = (file) => spawnSync("bash", [INSTALL, "--home", ok.home, "--personas", "codex-a", "--rust-bin", rustStub(ok.root, "e"), "--env-file", file], {
    encoding: "utf8",
    env: { PATH: `${path.join(ok.root, "bin")}:/usr/bin:/bin`, HOME: ok.root, FLEET_ENV_BIN: "/nonexistent/fleet-env" },
  });
  const r = run(good);
  assert.equal(r.status, 0, r.stderr);
  const unit = fs.readFileSync(path.join(ok.home, ".config", "systemd", "user", "codex-sidecar.service"), "utf8");
  assert.ok(unit.split("\n").includes(`EnvironmentFile=${good}`));
  assert.doesNotMatch(unit, /^(After|Requires)=fleet-env/m);
  assert.ok(!(r.stdout + r.stderr).includes(DUMMY));
  assert.doesNotMatch(ok.calls, /fleet-env/);
  const l = run(loose);
  assert.notEqual(l.status, 0);
  assert.match(l.stderr, /mode 0600/);
  assert.notEqual(run(nokey).status, 0);
  assert.notEqual(run("relative.env").status, 0);
  assert.notEqual(run(path.join(ok.root, "absent.env")).status, 0);
});

// A stub "binary" stands in for the release build (no cargo in tests).
function rustStub(root, tag) {
  const p = path.join(root, `stub-${tag}`);
  fs.writeFileSync(p, `#!/bin/sh\n# ${tag}\nexit 0\n`);
  fs.chmodSync(p, 0o755);
  return p;
}

test("render: ExecStart is the installed binary, no node path, binary 0755", () => {
  let stub;
  const r = setup({ prepare: ({ root }) => { stub = rustStub(root, "v1"); }, args: [] });
  // first run above installed the v0 stub; re-run with v1 against the same home
  const rr = spawnSync("bash", [INSTALL, "--home", r.home, "--rust-bin", stub], {
    encoding: "utf8",
    env: { PATH: `${path.join(r.root, "bin")}:/usr/bin:/bin`, HOME: r.root, FLEET_ENV_BIN: path.join(r.root, "bin", "fleet-env") },
  });
  assert.equal(rr.status, 0, rr.stderr);
  const unit = fs.readFileSync(path.join(r.home, ".config", "systemd", "user", "codex-sidecar.service"), "utf8");
  const dest = path.join(r.home, ".local", "lib", "codex-sidecar", "codex-sidecar");
  assert.match(unit, new RegExp(`^ExecStart=${dest.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`, "m"));
  assert.match(unit, new RegExp(`^Environment=PATH=${path.join(r.root, "bin").replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}:/usr/local/bin:/usr/bin:/bin$`, "m"));
  assert.doesNotMatch(unit, /\.nvm|node|@EXEC@|@PATH@/);
  assert.equal(fs.statSync(dest).mode & 0o777, 0o755);
  assert.match(fs.readFileSync(dest, "utf8"), /# v1/);
  assert.doesNotMatch(rr.stdout, /cargo build/);
});

test("reinstall keeps the previous build as codex-sidecar.prev", () => {
  let v1, v2;
  const r = setup({ args: ["--rust-bin", "PLACEHOLDER"], prepare: ({ root }) => { v1 = rustStub(root, "v1"); v2 = rustStub(root, "v2"); } });
  assert.notEqual(r.status, 0, "placeholder path is rejected");
  assert.match(r.stderr, /--rust-bin is not an executable file/);
  const env = { PATH: `${path.join(r.root, "bin")}:/usr/bin:/bin`, HOME: r.root, FLEET_ENV_BIN: path.join(r.root, "bin", "fleet-env") };
  for (const b of [v1, v2]) {
    const x = spawnSync("bash", [INSTALL, "--home", r.home, "--rust-bin", b], { encoding: "utf8", env });
    assert.equal(x.status, 0, x.stderr);
  }
  const lib = path.join(r.home, ".local", "lib", "codex-sidecar");
  assert.match(fs.readFileSync(path.join(lib, "codex-sidecar"), "utf8"), /# v2/);
  assert.match(fs.readFileSync(path.join(lib, "codex-sidecar.prev"), "utf8"), /# v1/);
  assert.deepEqual(fs.readdirSync(lib).sort(), ["codex-sidecar", "codex-sidecar.prev"], "no temp files left behind");
});
