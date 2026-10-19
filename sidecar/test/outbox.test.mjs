// outbox.test.mjs — the sidecar's half of the tracker event seam (plan
// `fleet-tools/plans/tracker-event-seam.md`, WP-2; append log per
// `fleet-tools/plans/outbox-append-log.md`, WP-2). The reader lives in another repo, so
// the line format is pinned by byte equality against its `serializeRow` in
// rs/tests/outbox.rs; this file keeps the black-box HTTP cases against the target binary.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { withSidecar, req, msgBody, SIMPLE_TURN } from "./helpers.mjs";

const scenario = { defaultTurn: SIMPLE_TURN };

// The billing line is the only wire witness of a subagent (see session.test.mjs).
const billing = subagent =>
  `x-anthropic-billing-header: cc_version=2.1.270.1d1; cc_entrypoint=sdk-cli;${subagent ? " cc_is_subagent=true;" : ""}`;
const agentBody = ({ sessionId, subagent = false, system = "You are a helpful coding assistant." }) =>
  msgBody({
    sessionId,
    system: [{ type: "text", text: billing(subagent) }, { type: "text", text: system }],
    messages: [{ role: "user", content: subagent ? "audit this plan" : "orchestrate the plan" }],
  });

// Every row of every day-file under the log dir derived from the outbox path
// (`<dirname(path)>/log/`, rs/src/outbox.rs log_dir_for).
const logDirFor = file => path.join(path.dirname(file), "log");
function readOutbox(file) {
  const dir = logDirFor(file);
  return fs.readdirSync(dir).filter(n => /^\d{4}-\d{2}-\d{2}\.jsonl$/.test(n)).sort()
    .flatMap(n => fs.readFileSync(path.join(dir, n), "utf8").split("\n").filter(Boolean).map(l => JSON.parse(l)));
}

test("a parent turn writes one turn.completed row to the day-file log", async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-sidecar-outbox-"));
  t.after(() => { try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ } });
  const file = path.join(dir, "outbox", "events.db");
  const h = await withSidecar({ scenario, env: { SIDECAR_OUTBOX_PATH: file } });
  t.after(() => h.close());

  const sid = "aaaaaaaa-1111-1111-1111-111111111111";
  const r = await req(h, "/codex-a/v1/messages", { body: agentBody({ sessionId: sid }) });
  assert.equal(r.status, 200, r.text);

  const rows = readOutbox(file);
  assert.equal(rows.length, 1, JSON.stringify(rows));
  const [row] = rows;
  assert.equal(row.kind, "turn.completed");
  assert.equal(row.harness, "codex-sidecar");
  assert.equal(row.session_id, sid);
  assert.equal(row.agent_id, null, "a parent key carries no agent id");
  assert.equal(row.leg, "codex-a");
  const { payload } = row;
  assert.equal(payload.stop_reason, "end_turn");
  assert.equal(payload.model, "gpt-5.6-luna");
  assert.ok(payload.threadId, "the codex thread id rides along");
  assert.ok(!JSON.stringify(payload).includes("orchestrate the plan"), "no message content in a row");
});

test("a subagent key writes child.spawned once, then its turns under an agent_id", async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-sidecar-outbox-"));
  t.after(() => { try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ } });
  const file = path.join(dir, "outbox", "events.db");
  const h = await withSidecar({ scenario, env: { SIDECAR_OUTBOX_PATH: file } });
  t.after(() => h.close());

  const sid = "bbbbbbbb-2222-2222-2222-222222222222";
  const body = agentBody({ sessionId: sid, subagent: true, system: "You are an adversarial plan auditor." });
  assert.equal((await req(h, "/codex-a/v1/messages", { body })).status, 200);

  const rows = readOutbox(file);
  assert.deepEqual(rows.map(r => r.kind), ["child.spawned", "turn.completed"]);
  const agentId = rows[0].agent_id;
  assert.match(agentId, /^[0-9a-f]{16}$/, "the subagent suffix is the discriminator hash");
  for (const row of rows) {
    assert.equal(row.session_id, sid, "a subagent row carries its parent's session id");
    assert.equal(row.agent_id, agentId);
    assert.equal(row.leg, "codex-a");
  }
});

test("with SIDECAR_OUTBOX_PATH unset the writer opens nothing", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());
  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 200, r.text);
  assert.equal(fs.existsSync(path.join(h.dir, "outbox")), false);
  assert.equal(h.log().filter(l => l.kind === "http").length, 1, "the turn is unaffected");
});

test("an unwritable outbox path drops rows without failing the turn", async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-sidecar-outbox-"));
  t.after(() => { try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ } });
  fs.writeFileSync(path.join(dir, "nofile"), "not a directory\n");

  const h = await withSidecar({ scenario, env: { SIDECAR_OUTBOX_PATH: path.join(dir, "nofile", "events.db") } });
  t.after(() => h.close());
  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 200, r.text);
  assert.equal(r.json.stop_reason, "end_turn");
  assert.equal(h.log().filter(l => l.kind === "http-error").length, 0, "the outbox is fail-open");
});
