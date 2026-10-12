// fleet-env.mjs — the router's side of the fleet-wide fleet-env cutover switch
// (fleet-tools plans/env-management.md, fleet_env/AGENTS.md). Not a test file.
//
// Transitional: the switch (`fleet-env enabled`: file ~/.config/fleet-env/cutover, override
// FLEET_ENV_CUTOVER=1|0) is off by default and every reader keeps the legacy
// <code-root>/fleet-tools/.env. ON: the default env file is the tmpfs projection
// `fleet-env path router`, and exec consumers re-exec under `fleet-env run <id>`. ON with
// the fleet-env binary or the projection missing fails loudly -- never a silent fallback.
// Prints paths only, never a value.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";

export const LEGACY_ROUTER_ENV_FILE = "<code-root>/fleet-tools/.env";

const bin = () => process.env.FLEET_ENV_BIN || "<code-root>/fleet-tools/fleet_env/bin/fleet-env";

function die(msg) {
  process.stderr.write(`fleet-env: ${msg}\n`);
  process.exit(1);
}

/** true = switch ON, false = OFF; exits non-zero if ON but the binary is missing. */
export function fleetEnvOn() {
  const b = bin();
  if (!existsSync(b)) {
    const o = process.env.FLEET_ENV_CUTOVER;
    const home = process.env.FLEET_ENV_HOME || `${process.env.HOME || homedir()}/.config/fleet-env`;
    if (o === "0" || (o !== "1" && !existsSync(`${home}/cutover`))) return false;
    die(`cutover is on but ${b} is missing; refusing the legacy env path`);
  }
  const r = spawnSync(b, ["enabled"], { stdio: "inherit" });
  if (r.status === 0) return true;
  if (r.status === 1) return false;
  return die("`fleet-env enabled` failed");
}

/** Path of a consumer's projection; exits non-zero if it cannot be resolved or is missing. */
export function fleetEnvPath(id) {
  const r = spawnSync(bin(), ["path", id], { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] });
  const p = (r.stdout || "").trim();
  if (r.status !== 0 || !p) die(`\`fleet-env path ${id}\` failed`);
  if (!existsSync(p)) die(`cutover is on but ${p} is missing; run fleet-env project`);
  return p;
}

/** ROUTER_ENV_FILE if set, else the router projection (switch ON) or the legacy file (OFF). */
export function routerEnvFile() {
  if (process.env.ROUTER_ENV_FILE) return process.env.ROUTER_ENV_FILE;
  return fleetEnvOn() ? fleetEnvPath("router") : LEGACY_ROUTER_ENV_FILE;
}

/**
 * Switch ON: re-exec this process under `fleet-env run <id>` (keys arrive in the environment)
 * and never return. Returns true when already running under `fleet-env run <id>`, false when
 * the switch is OFF.
 */
export function fleetEnvReexec(id) {
  if (process.env.FLEET_ENV_RUN === id) return true;
  if (!fleetEnvOn()) return false;
  const r = spawnSync(bin(), ["run", id, "--", process.execPath, ...process.argv.slice(1)], { stdio: "inherit" });
  process.exit(r.status ?? 1);
}
