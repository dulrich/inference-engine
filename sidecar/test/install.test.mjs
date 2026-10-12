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

function setup({ on, projection = true }) {
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
  const r = spawnSync("bash", [INSTALL, "--home", home], {
    encoding: "utf8",
    env: { PATH: `${bin}:${path.dirname(process.execPath)}:/usr/bin:/bin`, HOME: root, FLEET_ENV_BIN: path.join(bin, "fleet-env") },
  });
  const unit = path.join(home, ".config", "systemd", "user", "codex-sidecar.service");
  return {
    ...r,
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
