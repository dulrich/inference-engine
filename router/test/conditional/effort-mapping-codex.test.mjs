// effort-mapping-codex.test.mjs — black-box gate for the codex leg's effort precedence
// (plans/codex-leg-effort-and-auditor-unpinning.md WP-1; the sidecar counterpart of
// conditional/effort-mapping.test.mjs, which gates the local leg's patch 0007).
//
// BLACK BOX, same shape as effort-mapping.test.mjs (this directory) and tool-search-conformance.test.mjs: nothing
// here imports the sidecar. Every assertion is made over HTTP against the running router at
// :4813 plus the sidecar's own JSONL log on disk. When the router or the sidecar is unreachable
// these tests FAIL; they never skip. A gate that quietly disappears when its target is down is
// not a gate.
//
// METERED — this is why the flag exists. Each run bills two real turns against the operator's
// Codex subscription (the cheapest family, `gpt-5.6-luna`, two ~64-token answers). A test that
// silently spends the user's money on every commit-suite run is worse than
// no test, and a test that silently SKIPS itself is worse than none either. So without
// CODEX_METERED=1 the file registers exactly one test and that test FAILS, loudly, naming the
// flag — never `skip`, never green-by-absence. `smoke-router.sh` guards its one metered codex
// turn the same way (behind `--codex`); `router/AGENTS.md` "Gates" names this flag.
//
// What is asserted, and why a LOG-ROW assertion rather than a token gate: the reasoning-token
// delta between luna/low and luna/high on a one-word answer is small and noisy, while the
// sidecar's `http` row IS the contract this WP added (`effort`, `effortSource`). Two turns,
// `output_config.effort: "high"` then `"low"`, on a bare id whose family default is `low` — so
// the `high` turn proves the request field is read at all, and the `low` turn proves the source
// is reported as `request` rather than `default` even when the value coincides with the default.
//
// The correlator. The `http` row carries `reqId` (sidecar-minted, never returned to the client)
// and `key`. `key` is the Claude Code session id, which the sidecar reads out of the request's
// `metadata.user_id` (`sessionKeyOf`: the field is JSON — `{"session_id": "<uuid>"}` — and a
// body whose `user_id` does not parse as JSON falls back to a prompt hash and logs
// `weak-session-key`). So the join is: mint one fresh uuid per run, put it in
// `metadata.user_id.session_id`, and match the rows whose `key` is that uuid. The prompt also
// carries a `probe-<Date.now()>` nonce so the two turns are identifiable by eye in the log.
//
// CONDITIONAL — lives in router/test/conditional/, outside the per-commit
// `node --test router/test/*.test.mjs` glob. Trigger surface (router/AGENTS.md "Gates" trigger
// table): `sidecar/` effort/request handling; codex providers in router/gateway.config.json; any
// router patch; this file. Run with CODEX_METERED=1; a triggered run without it is reported as
// not run, naming CODEX_METERED.
//
// Auth. The gateway key comes from $GATEWAY_KEY or $ROUTER_ENV_FILE (default
// <code-root>/fleet-tools/.env) and is never printed.
//
// Env: CODEX_METERED (must be "1" to run the metered cases), ROUTER_BASE_URL
//      (default http://127.0.0.1:4813), ROUTER_ENV_FILE, GATEWAY_KEY,
//      SIDECAR_LOG_DIR (default ~/.local/state/codex-sidecar/logs — the sidecar's own default).

import { routerEnvFile } from "../../fleet-env.mjs";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const METERED = process.env.CODEX_METERED === "1";
const BASE_URL = (process.env.ROUTER_BASE_URL || "http://127.0.0.1:4813").replace(/\/$/, "");
// ROUTER_ENV_FILE, else `fleet-env path router` under the fleet-env cutover, else the legacy file.
const ENV_FILE = routerEnvFile();
const LOG_DIR = process.env.SIDECAR_LOG_DIR
  || path.join(process.env.HOME || os.homedir(), ".local", "state", "codex-sidecar", "logs");
// The cheapest family on the leg, and the one the per-turn effort measurement was taken on.
const MODEL = "codex-b/gpt-5.6-luna";
// The engine's anthropic-type legs want *some* client bearer to build passthrough headers.
const DUMMY_BEARER = "effort-mapping-codex-dummy-client-bearer";
const REQUEST_TIMEOUT_MS = 300000;
const TEST_TIMEOUT_MS = 600000;
// The sidecar writes its row after the response is finished; give the fd a moment either way.
const LOG_SETTLE_MS = 2000;

const SESSION_ID = crypto.randomUUID();
const NONCE = `probe-${Date.now()}`;

if (!METERED) {
  test("codex effort mapping is a METERED gate and did not run", () => {
    assert.fail("not run — set CODEX_METERED=1 (bills the Codex subscription)");
  });
} else {
  run();
}

function run() {
  test("output_config.effort high reaches the sidecar as effort=high, effortSource=request",
    { timeout: TEST_TIMEOUT_MS }, async () => {
      const r = await messages(probeBody("high"));
      assertTextAnswer(r, "effort high");
      const row = await httpRow(0, "high");
      console.log(`[codex-effort] high row: ${JSON.stringify(pick(row))}`);
      assert.equal(row.effort, "high", `the sidecar ran the turn at ${row.effort}, not high`);
      assert.equal(row.effortSource, "request", `effortSource was ${row.effortSource}, not request`);
    });

  test("output_config.effort low is reported as request, not as the luna family default",
    { timeout: TEST_TIMEOUT_MS }, async () => {
      // `low` IS luna's family default, so only `effortSource` distinguishes "the harness asked
      // for low" from "nobody asked and the default applied" — which is the whole point of the field.
      const r = await messages(probeBody("low"));
      assertTextAnswer(r, "effort low");
      const row = await httpRow(1, "low");
      console.log(`[codex-effort] low  row: ${JSON.stringify(pick(row))}`);
      assert.equal(row.effort, "low", `the sidecar ran the turn at ${row.effort}, not low`);
      assert.equal(row.effortSource, "request", `effortSource was ${row.effortSource}, not request`);
      // Both turns ran on one session key, so the change is also on the record — same thread,
      // one `effort-changed` row, which is the per-turn mechanism stated as an assertion.
      const changed = readRows().filter(l => l.kind === "effort-changed");
      console.log(`[codex-effort] effort-changed: ${JSON.stringify(changed)}`);
      assert.equal(changed.length, 1, `expected exactly one effort-changed row, got ${changed.length}`);
      assert.deepEqual([changed[0].from, changed[0].to, changed[0].source], ["high", "low", "request"]);
      assert.equal(changed[0].threadId, row.threadId, "the effort change must stay on the same thread");
    });
}

function probeBody(effort) {
  return {
    model: MODEL,
    max_tokens: 64,
    system: "You answer in exactly one word.",
    messages: [{ role: "user", content: `${NONCE}: reply with the single word pong` }],
    output_config: { effort },
    metadata: { user_id: JSON.stringify({ session_id: SESSION_ID }) },
  };
}

function gatewayKey() {
  if (process.env.GATEWAY_KEY) return process.env.GATEWAY_KEY;
  let text = "";
  try {
    text = fs.readFileSync(ENV_FILE, "utf8");
  } catch (err) {
    assert.fail(`no gateway key: $GATEWAY_KEY is unset and ${ENV_FILE} is unreadable (${err.code}). `
      + "Set GATEWAY_KEY or point ROUTER_ENV_FILE at the env file holding INFERENCE_ROUTER_GATEWAY_KEY.");
  }
  const hit = /^INFERENCE_ROUTER_GATEWAY_KEY=(.*)$/m.exec(text);
  if (!hit || !hit[1].trim()) {
    assert.fail(`${ENV_FILE} carries no INFERENCE_ROUTER_GATEWAY_KEY; set $GATEWAY_KEY instead.`);
  }
  return hit[1].trim();              // never logged, never asserted on
}

async function messages(body, { timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`${BASE_URL}/v1/messages`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-gateway-key": gatewayKey(),
        "anthropic-version": "2023-06-01",
        authorization: `Bearer ${DUMMY_BEARER}`,
      },
      body: JSON.stringify(body),
      signal: ac.signal,
    });
  } catch (err) {
    assert.fail(`gateway at ${BASE_URL} is unreachable (POST /v1/messages): ${err.name}: ${err.message}. `
      + "This test is a black-box gate against the router and the codex sidecar behind it, and "
      + "fails rather than skips when either is down. Start them, or point ROUTER_BASE_URL elsewhere.");
  } finally {
    clearTimeout(timer);
  }
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* not json */ }
  return { status: res.status, text, json };
}

function assertTextAnswer(r, label) {
  assert.ok(r.status >= 200 && r.status < 300, `${label}: expected 2xx, got ${r.status}: ${r.text.slice(0, 400)}`);
  const blocks = Array.isArray(r.json?.content) ? r.json.content : [];
  assert.ok(blocks.some(b => b?.type === "text" && typeof b.text === "string"),
    `${label}: response carried no text block: ${r.text.slice(0, 400)}`);
}

// Today's sidecar log, newest row first, restricted to this run's session key.
function readRows() {
  const day = new Date().toISOString().slice(0, 10);
  const file = path.join(LOG_DIR, `codex-sidecar-${day}.jsonl`);
  let text = "";
  try {
    text = fs.readFileSync(file, "utf8");
  } catch (err) {
    assert.fail(`the sidecar log ${file} is unreadable (${err.code}). This gate reads the row the `
      + "sidecar wrote for its own turn; without it there is nothing to assert. Set SIDECAR_LOG_DIR "
      + "if the sidecar runs with a non-default state dir.");
  }
  return text.split("\n").filter(Boolean)
    .map(l => { try { return JSON.parse(l); } catch { return null; } })
    .filter(l => l && String(l.key ?? "").includes(SESSION_ID));
}

// The `nth` (0-based) `http` row this run produced, waited for: the row is written after the
// HTTP response is flushed, so a read immediately after `await messages(...)` can race it — and
// an ordinal rather than `.at(-1)` so a not-yet-written row can never be mistaken for the
// previous test's row.
async function httpRow(nth, effort) {
  const deadline = Date.now() + LOG_SETTLE_MS;
  for (;;) {
    const rows = readRows();
    const hit = rows.filter(l => l.kind === "http")[nth];
    if (hit) return hit;
    if (Date.now() > deadline) {
      const kinds = rows.map(l => l.kind);
      assert.fail(`no sidecar \`http\` row #${nth} for session ${SESSION_ID} (effort ${effort}) within `
        + `${LOG_SETTLE_MS} ms. Rows seen for this key: ${JSON.stringify(kinds)}. A \`weak-session-key\` `
        + "row instead means metadata.user_id did not survive to the sidecar; an `http-error` row "
        + "carries the upstream failure.");
    }
    await new Promise(r => setTimeout(r, 100));
  }
}

const pick = row => ({
  kind: row.kind, model: row.model, effort: row.effort, effortSource: row.effortSource,
  reqId: row.reqId, key: row.key, mode: row.mode, threadId: row.threadId, stop_reason: row.stop_reason,
});
