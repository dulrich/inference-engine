// tool-search.test.mjs — the deferred-tool (ENABLE_TOOL_SEARCH) path across the codex leg.
//
// Claude Code advertises the deferred-tool name list, the agent-type listing and the
// installed-skills listing ONLY inside `role:"system"` entries interleaved in `messages[]`
// (measured: docs/tool-calling-gateway/tool-search-conformance-spike.md). `conversational`
// drops those entries from the turn, so their text has to reach the thread another way or the
// model is handed a working `ToolSearch` tool with nothing to search for.
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { withSidecar, req, msgBody, sentTo, envMessage } from "./helpers.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PREAMBLE = fs.readFileSync(path.join(HERE, "..", "preamble.txt"), "utf8").replace(/\n+$/, "");
const SYSTEM = "You are a helpful coding assistant.";

// The advertisement verbatim from the spike capture (`codex-on/001.req.json`).
const ADVERT = `The following deferred tools are now available via ToolSearch. Their schemas are NOT loaded — calling them directly will fail with InputValidationError. Use ToolSearch with query "select:<name>[,<name>...]" to load tool schemas before calling them:
NotebookEdit / SendMessage / TaskStop`;

const advertMessage = { role: "system", content: [{ type: "text", text: ADVERT }] };

// Request 1's catalogue shape: a fully-schema'd ToolSearch, exactly one `defer_loading: true`
// placeholder, and the always-on tools. The deferred tools themselves are absent.
const TOOL_SEARCH = {
  name: "ToolSearch",
  description: "Fetches full schema definitions for deferred tools so they can be called.",
  input_schema: { type: "object", properties: { query: { type: "string" }, max_results: { type: "number" } }, required: ["query"] },
};
const PLACEHOLDER = {
  name: "DeferredToolPlaceholder",
  description: "Reserved placeholder that keeps deferred tool loading active; never call this tool.",
  input_schema: { type: "object", properties: {} },
  defer_loading: true,
};
const BASH = { name: "Bash", description: "run a command", input_schema: { type: "object" } };
const DEFERRED_ON = [TOOL_SEARCH, PLACEHOLDER, BASH];

// After discovery the array grows by exactly one entry, which keeps `defer_loading: true`.
const NOTEBOOK_EDIT = {
  name: "NotebookEdit",
  description: "Replace a cell in a Jupyter notebook.",
  input_schema: {
    type: "object",
    properties: { notebook_path: { type: "string" }, new_source: { type: "string" }, cell_id: { type: "string" } },
    required: ["notebook_path", "new_source"],
  },
  defer_loading: true,
};
const GROWN = [...DEFERRED_ON, NOTEBOOK_EDIT];

const call = (callId, tool, args) => ({ request: "item/tool/call", params: { callId, tool, arguments: args } });
const toolResponses = h => h.trace().filter(e => e.kind === "in" && e.frame?.result?.contentItems).map(e => e.frame.result);

test("the deferred-tool advertisement in a role:\"system\" message reaches the thread's baseInstructions", async t => {
  const h = await withSidecar({ scenario: { turns: [[{ text: "ok" }, { complete: {} }]] } });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      system: SYSTEM,
      tools: DEFERRED_ON,
      // Claude Code's own order: the user turn first, then the interleaved system entries.
      messages: [{ role: "user", content: "what parameters does NotebookEdit take?" }, advertMessage, envMessage(h.dir)],
    }),
  });
  assert.equal(r.status, 200, r.text);

  const started = sentTo(h.trace(), "thread/start");
  assert.equal(started.length, 1);
  const base = started[0].params.baseInstructions;

  // (a) the advertisement survives, verbatim — this is the whole defect.
  assert.ok(base.includes(ADVERT), "the deferred-tool name list reached baseInstructions");
  assert.ok(base.includes("NotebookEdit"), "the deferred tool's name is visible to the model");
  // (b) so does every other interleaved entry, in message order, after the top-level system text.
  assert.ok(base.includes("Primary working directory:"), "the environment entry survives too");
  assert.ok(base.startsWith(`${PREAMBLE}\n\n${SYSTEM}\n\n`), "preamble, then top-level system, then the interleaved text");
  assert.ok(base.indexOf(ADVERT) < base.indexOf("Primary working directory:"), "interleaved entries keep messages[] order");

  // (c) nothing else moved: cwd still comes from the environment entry, the catalogue is intact,
  // and the log still counts the dropped system-role messages.
  assert.equal(started[0].params.cwd, h.dir, "extractCwd is unaffected");
  assert.deepEqual(started[0].params.dynamicTools.map(d => d.name), ["ToolSearch", "DeferredToolPlaceholder", "Bash"]);
  const http = h.log().find(l => l.kind === "http");
  assert.equal(http.nSystemRole, 2, "the nSystemRole count still counts both dropped entries");
  assert.equal(http.nTools, 3);
  const verified = h.log().find(l => l.kind === "config-verified");
  assert.equal(verified.interleavedSystemChars, `${ADVERT}\n\n${envMessage(h.dir).content[0].text}`.length);
});

test("a ToolSearch round-trip relays the tool_reference unchanged and the grown catalogue's tool executes", async t => {
  const h = await withSidecar({
    scenario: {
      turns: [[
        call("call_ts", "ToolSearch", { query: "select:NotebookEdit", max_results: 1 }),
        { awaitToolResults: 1 },
        call("call_ne", "NotebookEdit", { notebook_path: "/tmp/probe.ipynb", new_source: "1+1" }),
        { awaitToolResults: 1 },
        { text: "edited" },
        { complete: {} },
      ]],
    },
  });
  t.after(() => h.close());

  // 1. discovery call, against the un-grown 3-tool catalogue
  const first = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ system: SYSTEM, tools: DEFERRED_ON, messages: [{ role: "user", content: "edit the notebook" }, advertMessage] }),
  });
  assert.equal(first.status, 200, first.text);
  assert.equal(first.json.stop_reason, "tool_use");
  const search = first.json.content.find(b => b.type === "tool_use");
  assert.deepEqual(
    { name: search.name, input: search.input },
    { name: "ToolSearch", input: { query: "select:NotebookEdit", max_results: 1 } },
  );

  // 2. the harness answers with a tool-reference block and grows `tools` 3 -> 4
  const reference = [{ type: "tool_reference", tool_name: "NotebookEdit" }];
  const second = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      system: SYSTEM,
      tools: GROWN,
      messages: [
        { role: "user", content: "edit the notebook" },
        advertMessage,
        { role: "assistant", content: first.json.content },
        { role: "user", content: [{ type: "tool_result", tool_use_id: search.id, content: reference }] },
      ],
    }),
  });
  assert.equal(second.status, 200, second.text);
  assert.equal(second.json.stop_reason, "tool_use");
  const concrete = second.json.content.find(b => b.type === "tool_use");
  assert.equal(concrete.name, "NotebookEdit", "the concrete deferred tool is the one that runs");
  assert.equal(concrete.input.notebook_path, "/tmp/probe.ipynb");

  // the reference block is relayed to the app-server unchanged (no text block to flatten)
  assert.equal(toolResponses(h)[0].contentItems[0].text, JSON.stringify(reference));
  assert.equal(toolResponses(h)[0].success, true);

  // 3. the concrete tool's result closes the turn
  const third = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      system: SYSTEM,
      tools: GROWN,
      messages: [
        { role: "user", content: "edit the notebook" },
        advertMessage,
        { role: "assistant", content: first.json.content },
        { role: "user", content: [{ type: "tool_result", tool_use_id: search.id, content: reference }] },
        { role: "assistant", content: second.json.content },
        { role: "user", content: [{ type: "tool_result", tool_use_id: concrete.id, content: "cell replaced" }] },
      ],
    }),
  });
  assert.equal(third.status, 200, third.text);
  assert.equal(third.json.stop_reason, "end_turn");
  assert.equal(third.json.content[0].text, "edited");

  assert.equal(sentTo(h.trace(), "thread/start").length, 1, "the whole discovery round-trip stayed on one thread");
  assert.ok(!h.log().some(l => l.kind === "orphan-tool-result"));
});

test("with no interleaved system entry baseInstructions is byte-identical to preamble + system", async t => {
  const h = await withSidecar({ scenario: { turns: [[{ text: "ok" }, { complete: {} }]] } });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ system: SYSTEM, tools: [BASH], messages: [{ role: "user", content: "hi" }] }),
  });
  assert.equal(r.status, 200, r.text);

  const started = sentTo(h.trace(), "thread/start");
  assert.equal(started[0].params.baseInstructions, `${PREAMBLE}\n\n${SYSTEM}`, "no trailing separator when nothing was interleaved");
  const http = h.log().find(l => l.kind === "http");
  assert.equal(http.nSystemRole, 0);
  assert.equal(h.log().find(l => l.kind === "config-verified").interleavedSystemChars, 0);
});
