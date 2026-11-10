// test-slots.mjs — the one place the live tests read their model slots from (the repo-root
// test-slots.json; $TEST_SLOT_<NAME> overrides one slot for one run). The smoke scripts read the
// same file through scripts/lib/test-slots.sh. Not a test file: the `*.test.mjs` glob skips it.

import fs from "node:fs";
import { fileURLToPath } from "node:url";

export const SLOT_FILE = fileURLToPath(new URL("../../test-slots.json", import.meta.url));
const KNOWN = ["ANTHROPIC_CHEAP", "CODEX_SIDECAR_CHEAP", "LOCAL_SMALL", "LOCAL_EFFORT", "OPENROUTER_ADD_PROBE"];

/** The value of slot `name`; `$TEST_SLOT_<NAME>` (non-empty) overrides. Throws a `test-slots:` Error when unusable. */
export function testSlot(name) {
  if (!KNOWN.includes(name)) throw new Error(`test-slots: unknown slot '${name}' (known: ${KNOWN.join(" ")})`);
  const ovr = `TEST_SLOT_${name}`;
  const fromEnv = process.env[ovr];
  let val = fromEnv;
  let src = `the ${ovr} override`;
  if (!val) {
    src = SLOT_FILE;
    try {
      val = JSON.parse(fs.readFileSync(SLOT_FILE, "utf8"))?.[name];
    } catch {
      throw new Error(`test-slots: slot ${name} is unreadable: the file is missing or not JSON (${src})`);
    }
    if (typeof val !== "string") throw new Error(`test-slots: slot ${name} is missing or not a string (${src})`);
  }
  if (!val) throw new Error(`test-slots: slot ${name} is empty (${src})`);
  if (val.includes("/") && name !== "OPENROUTER_ADD_PROBE") {
    throw new Error(`test-slots: slot ${name} must be a bare id without a '/', got '${val}' (${src})`);
  }
  return val;
}

/**
 * The codex persona the live tests send turns to: $CODEX_TEST_PERSONA, else (when `ids`, the
 * router's /v1/models id list, is given) the first `codex-*` provider listed. Fails loudly when
 * neither names one -- never a silent default.
 */
export function codexPersona(ids) {
  const fromEnv = process.env.CODEX_TEST_PERSONA?.trim();
  if (fromEnv) return fromEnv;
  const hit = (ids ?? []).map(id => /^(codex-[^/]+)\//.exec(id)?.[1]).find(Boolean);
  if (hit) return hit;
  throw new Error("test-slots: no codex persona: set $CODEX_TEST_PERSONA (or list a codex-* provider in /v1/models)");
}

/** The one "not served" message; consumers fail the leg with it. */
export function slotNotServed(name, value) {
  return `slot ${name}=${value} is not served — edit test-slots.json`;
}
