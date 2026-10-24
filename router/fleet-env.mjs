// fleet-env.mjs — the router's side of the fleet-wide fleet-env store
// (fleet-tools plans/env-management.md, fleet_env/AGENTS.md). Not a test file.
//
// Store-only: there is no switch and no legacy env file. The default env file is the tmpfs
// projection `fleet-env path router`, and exec consumers re-exec under `fleet-env run <id>`.
// The fleet-env binary or the projection missing fails loudly -- never a silent fallback.
// Prints paths only, never a value.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";

const bin = () => process.env.FLEET_ENV_BIN || "<code-root>/fleet-tools/fleet/fleet_env/bin/fleet-env";

function die(msg) {
  process.stderr.write(`fleet-env: ${msg}\n`);
  process.exit(1);
}

function requireBin() {
  const b = bin();
  if (!existsSync(b)) die(`${b} is missing; refusing to run without credentials`);
  return b;
}

/** Path of a consumer's projection; exits non-zero if it cannot be resolved or is missing. */
export function fleetEnvPath(id) {
  const r = spawnSync(requireBin(), ["path", id], { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] });
  const p = (r.stdout || "").trim();
  if (r.status !== 0 || !p) die(`\`fleet-env path ${id}\` failed`);
  if (!existsSync(p)) die(`${p} is missing; run fleet-env project`);
  return p;
}

/** ROUTER_ENV_FILE if set (operator override of the path), else the router projection. */
export function routerEnvFile() {
  if (process.env.ROUTER_ENV_FILE) return process.env.ROUTER_ENV_FILE;
  return fleetEnvPath("router");
}

/**
 * Re-exec this process under `fleet-env run <id>` (keys arrive in the environment) and never
 * return. Returns true only when already running under `fleet-env run <id>`; exits non-zero
 * if the binary is missing.
 */
export function fleetEnvReexec(id) {
  if (process.env.FLEET_ENV_RUN === id) return true;
  const r = spawnSync(requireBin(), ["run", id, "--", process.execPath, ...process.argv.slice(1)], { stdio: "inherit" });
  process.exit(r.status ?? 1);
}
