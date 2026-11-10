// tool-search-shared.mjs — helpers shared by the deferred-tool (ENABLE_TOOL_SEARCH) conformance
// files: router/test/tool-search-conformance.test.mjs (offline fixture self-check, default glob),
// router/test/conditional/tool-search-conformance-live.test.mjs (local + unmetered codex legs, local relay),
// router/test/conditional/tool-search-hosted.test.mjs (hosted passthrough leg, ROUTER_HOSTED_BEARER)
// and router/test/conditional/tool-search-metered.test.mjs (codex relay). The gateway client lives in ./gateway-client.mjs;
// importing this module reads no key and touches no network.
// Not a test file: the `*.test.mjs` glob does not load it. The method notes (bodies, token metric,
// auth, fail-never-skip) live in the header of tool-search-conformance.test.mjs and the live file.

import { codexPersona, slotNotServed, testSlot } from "../../scripts/lib/test-slots.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import { DUMMY_BEARER, call, envFile, modelIds } from "./gateway-client.mjs";
import test from "node:test";

const ADVERT_PHRASE = "deferred tools are now available via ToolSearch";
const PROBE_TOOL = "NotebookEdit";

// Resolution rule: every leg resolves its slot id (test-slots.json) exactly or fails loudly with the
// slotNotServed text — there is no fallback to another model.
function resolveModel(ids, { preferred, listedAs = preferred, slot }) {
  if (ids.includes(listedAs)) return { id: preferred, exact: true };
  throw new Error(`${slotNotServed(slot, preferred.slice(preferred.lastIndexOf("/") + 1))} (GET /v1/models does not list ${listedAs})`);
}

// ─────────────────────────────────────────────────────────────────────────────── request bodies

const TOOL_SEARCH_TOOL = {
  name: "ToolSearch",
  description: "Fetches full schema definitions for deferred tools so they can be called.",
  input_schema: {
    type: "object",
    properties: { query: { type: "string" }, max_results: { type: "number" } },
    required: ["query"],
  },
};
const PLACEHOLDER_TOOL = {
  name: "DeferredToolPlaceholder",
  description: "Reserved placeholder that keeps deferred tool loading active; never call this tool.",
  input_schema: { type: "object", properties: {} },
  defer_loading: true,
};
const ALWAYS_ON = ["Agent", "Bash", "Edit", "ListAgents", "Read", "ReportFindings", "ScheduleWakeup", "Skill", "Workflow", "Write"]
  .map(name => ({
    name,
    description: `The ${name} tool, always loaded in this profile.`,
    input_schema: { type: "object", properties: { input: { type: "string" } } },
  }));
const DEFERRED_NAMES = ["CronCreate", "CronDelete", "CronList", "DesignSync", "EnterWorktree",
  "ExitWorktree", PROBE_TOOL, "SendMessage", "TaskOutput", "TaskStop"];

const ADVERT = `The following deferred tools are now available via ToolSearch. Their schemas are NOT loaded — `
  + `calling them directly will fail with InputValidationError. Use ToolSearch with query `
  + `"select:<name>[,<name>...]" to load tool schemas before calling them:\n${DEFERRED_NAMES.join(" / ")}`;

const SYNTH_SYSTEM = [{ type: "text", text: "You are Claude Code, Anthropic's official CLI for Claude." }];

// ENABLE_TOOL_SEARCH=true: 12 tools, the deferred ones absent from `tools` and named in prose.
const synthOn = () => ({
  system: SYNTH_SYSTEM,
  tools: [TOOL_SEARCH_TOOL, PLACEHOLDER_TOOL, ...ALWAYS_ON],
  messages: [
    { role: "user", content: `What parameters does ${PROBE_TOOL} take?` },
    { role: "system", content: [{ type: "text", text: ADVERT }] },
  ],
});

// ENABLE_TOOL_SEARCH=false: the catalogue is eagerly materialised — no ToolSearch, no
// defer_loading, every deferred tool carrying its full schema.
const synthOff = () => ({
  system: SYNTH_SYSTEM,
  tools: [
    ...ALWAYS_ON,
    ...DEFERRED_NAMES.map(name => ({
      name,
      description: `The ${name} tool, eagerly loaded because tool search is disabled. `.repeat(4),
      input_schema: {
        type: "object",
        properties: { path: { type: "string" }, content: { type: "string" }, mode: { type: "string" } },
        required: ["path"],
      },
    })),
  ],
  messages: [{ role: "user", content: `What parameters does ${PROBE_TOOL} take?` }],
});

// The captured id is whatever was resident on spike day; the leg is what matters, so the model
// is always rewritten to the id this gateway reports now.
const countBody = (model, src, { withTools = true } = {}) => ({
  model,
  system: src.system,
  messages: [{ role: "user", content: "hi" }],
  ...(withTools ? { tools: src.tools } : {}),
});

// ──────────────────────────────────────────────────────────────────────────────────── the legs

const LEGS = [
  {
    cell: "passthrough",
    label: "Claude passthrough",
    hosted: true,
    pick: ids => {
      const slot = testSlot("ANTHROPIC_CHEAP");
      return resolveModel(ids, { preferred: slot, listedAs: `anthropic/${slot}`, slot: "ANTHROPIC_CHEAP" });
    },
    // Synthetic bodies on the exact model. Measured 2026-10-19 on claude-haiku-4-5: system+tools on 1110 /
    // off 2330, tools-only ratio 2.12x. Re-measured 2026-11-10 on claude-haiku-5-5 (the ANTHROPIC_CHEAP
    // slot): system+tools on 1223 / off 2945, tools-only 1189 / 2911, ratio 2.45x (floor 2.2x, ~10% slack).
    synth: { minRatio: 2.2 },
  },
  {
    cell: "local",
    label: "local OpenAI-conversion",
    pick: ids => {
      const slot = testSlot("LOCAL_SMALL");
      return resolveModel(ids, { preferred: `claude-local/${slot}`, slot: "LOCAL_SMALL" });
    },
    // Synthetic bodies, measured 2026-10-19 on nemotron-4b: on 897 / off 2233, tools-only 866/2202,
    // ratio 2.54x (floor 2.3x, ~10% slack). Only the ratio is asserted (see the token gate).
    synth: { minRatio: 2.3 },
  },
  {
    cell: "codex",
    label: "codex sidecar",
    pick: ids => {
      const slot = testSlot("CODEX_SIDECAR_CHEAP");
      const preferred = `${codexPersona(ids)}/${slot}`;
      if (!ids.includes(preferred)) throw new Error(`${slotNotServed("CODEX_SIDECAR_CHEAP", slot)} (GET /v1/models does not list ${preferred})`);
      return { id: preferred, exact: true };
    },
    // Synthetic bodies, measured 2026-10-19 on gpt-5.6-luna and re-measured identical on gpt-6-luna
    // 2026-10-31: system+tools on 382 / off 1344, tools-only ratio 3.62x (floor 3.2x, ~10% slack).
    // Only the ratio is asserted (see the token gate).
    synth: { minRatio: 3.2 },
  },
];

// $ROUTER_HOSTED_BEARER, else the router env file's ROUTER_HOSTED_BEARER line (resolved like
// gatewayKey()). Missing in both fails the cell rather than skipping it.
function hostedBearer() {
  const fromEnv = process.env.ROUTER_HOSTED_BEARER;
  if (fromEnv && fromEnv.trim()) return fromEnv.trim();
  let text = "";
  try {
    text = fs.readFileSync(envFile(), "utf8");
  } catch {
    text = "";
  }
  const hit = /^ROUTER_HOSTED_BEARER=(.*)$/m.exec(text);
  assert.ok(hit && hit[1].trim(), "the hosted passthrough leg forwards the caller's own OAuth bearer, and the gateway key alone "
    + "gets \"ANTHROPIC_API_KEY is missing.\" (measured, WP-5). Set $ROUTER_HOSTED_BEARER, or a ROUTER_HOSTED_BEARER line in "
    + `${envFile()} (ROUTER_ENV_FILE overrides the path), to a bearer from an authenticated session before running the `
    + "passthrough cells. This cell fails rather than skips on purpose: a silently-skipped conformance cell is the "
    + "failure mode this file exists to prevent.");
  return hit[1].trim();          // never logged, never asserted on
}

async function countTokens(leg, model, src, opts = {}) {
  const bearer = leg.hosted ? hostedBearer() : DUMMY_BEARER;
  const r = await call("/v1/messages/count_tokens", countBody(model, src, opts), { bearer });
  assert.equal(r.status, 200, `count_tokens on the ${leg.label} leg returned ${r.status}: ${r.text.slice(0, 400)}${r.hint}`);
  const n = r.json?.input_tokens;
  assert.equal(typeof n, "number", `count_tokens on the ${leg.label} leg returned no integer input_tokens: ${r.text.slice(0, 400)}`);
  return n;
}

const deferredEntries = tools => tools.filter(t => t && t.defer_loading === true);
const byName = (tools, name) => tools.find(t => t?.name === name);
// Registers the fixture self-check ONCE (not per leg): the synthetic bodies carry the deferred shape and
// the eager control does not. Sends nothing — it is the offline cell of the default glob.
export function registerDeferredShapeCheck() {
  test("request 1 carries the deferred shape, and the eager control does not (fixture self-check)", () => {
    const onSrc = synthOn();
    const offSrc = synthOff();

    // Invariant 1 — exactly one defer_loading marker, a usable ToolSearch, the probe tool absent.
    const deferred = deferredEntries(onSrc.tools);
    assert.equal(deferred.length, 1, `request 1 must carry exactly one defer_loading entry, got ${deferred.length}: `
      + `${deferred.map(d => d.name).join(", ")}`);
    assert.equal(deferred[0].name, "DeferredToolPlaceholder");
    const search = byName(onSrc.tools, "ToolSearch");
    assert.ok(search, "request 1 must carry a ToolSearch tool");
    assert.ok(search.input_schema && Object.keys(search.input_schema).length > 0, "ToolSearch must carry a non-empty input_schema");
    assert.equal(byName(onSrc.tools, PROBE_TOOL), undefined, `${PROBE_TOOL} must be absent from request 1's tools`);

    // The deferred names reach the model only in prose, never in `tools`.
    const advertised = JSON.stringify(onSrc.messages ?? []) + JSON.stringify(onSrc.system ?? "");
    assert.ok(advertised.includes(ADVERT_PHRASE), "the deferred-tool name list must be advertised in the request");
    assert.ok(!JSON.stringify(onSrc.tools).includes(ADVERT_PHRASE), "the advertisement belongs in prose, not in `tools`");

    // Negative control — the same assertions must fail on an eager profile.
    assert.equal(deferredEntries(offSrc.tools).length, 0, "the eager control must carry no defer_loading entry");
    assert.equal(byName(offSrc.tools, "ToolSearch"), undefined, "the eager control must carry no ToolSearch tool");
    assert.ok(offSrc.tools.length > onSrc.tools.length, "the eager control must materialise a larger catalogue");
  });
}

// Registers the per-leg within-leg token gate (live: count_tokens against the gateway) for one leg.
export function registerLegTests(leg) {
  test(`${leg.label}: the deferred catalogue is not eagerly expanded (within-leg token gate)`, async t => {
    const ids = await modelIds();
    const { id: model } = leg.pick(ids);
    const onSrc = synthOn();
    const offSrc = synthOff();

    const onTokens = await countTokens(leg, model, onSrc);
    const offTokens = await countTokens(leg, model, offSrc);
    const onToolsOnly = onTokens - await countTokens(leg, model, onSrc, { withTools: false });
    const offToolsOnly = offTokens - await countTokens(leg, model, offSrc, { withTools: false });
    const ratio = offToolsOnly / onToolsOnly;
    t.diagnostic(`${leg.cell} (${model}): system+tools on=${onTokens} off=${offTokens} | `
      + `tools only on=${onToolsOnly} off=${offToolsOnly} | ratio ${ratio.toFixed(2)}x | synthetic bodies`);

    // Ordering, every leg: deferral must save something, on the whole prompt and the tool block.
    assert.ok(offTokens > onTokens, `${leg.label}: the eager control (${offTokens}) did not cost more than the deferred `
      + `profile (${onTokens}) — the deferred path is saving nothing.`);
    assert.ok(offToolsOnly > onToolsOnly, `${leg.label}: the eager tool block (${offToolsOnly}) did not exceed the deferred `
      + `one (${onToolsOnly}); deferral is not reducing the tool block at all.`);

    // The claim is "deferred tools are cheaper than eager tools", and the tools-only ratio carries it: a
    // partial eager expansion lowers the ratio below the floor. Absolute on/off counts only restated one
    // model's tokenizer (they broke on every hosted model bump), so they are not asserted; the measured
    // figures stay in LEGS[].synth comments as history. The floor is measured per exact model.
    const { minRatio } = leg.synth ?? {};
    if (!minRatio) {
      t.diagnostic(`${leg.cell}: no measured ratio floor; ordering only.`);
      return;
    }
    // Ratio floor (~10% slack under the measured saving): a partial eager expansion fails here
    // even when plain ordering still holds.
    assert.ok(ratio >= minRatio, `${leg.label}: the eager/deferred tool-block ratio is ${ratio.toFixed(2)}x `
      + `(${offToolsOnly}/${onToolsOnly}), below the floor ${minRatio}x — deferral saves less than measured; part of `
      + "the deferred catalogue is being counted eagerly, or the synthetic bodies changed.");
  });
}

// ───────────────────────────────────────────────────────── relay of the discovery round-trip

// Layer 1 asserts that the wire shapes of the discovery round-trip survive the leg: a
// tool_reference tool_result and a grown catalogue are accepted and answered normally, with no
// 4xx, no "tool not found" and no malformed continuation. Whether a given model *chooses* to
// call ToolSearch is a behavioural property and belongs to router/smoke-tool-search.sh, which
// drives a real `claude -p` turn.
function roundTripBody(model) {
  const src = synthOn();
  return {
    model,
    max_tokens: 64,
    stream: false,
    system: src.system,
    metadata: { user_id: JSON.stringify({ session_id: "tool-search-conformance-layer1" }) },
    tools: [...src.tools, {
      name: PROBE_TOOL,
      description: "Replace a cell in a Jupyter notebook.",
      input_schema: {
        type: "object",
        properties: { notebook_path: { type: "string" }, new_source: { type: "string" } },
        required: ["notebook_path", "new_source"],
      },
      defer_loading: true,            // retained after discovery, measured on both clean legs
    }],
    messages: [
      { role: "user", content: `What parameters does ${PROBE_TOOL} take?` },
      { role: "system", content: [{ type: "text", text: ADVERT }] },
      { role: "assistant", content: [{ type: "tool_use", id: "toolu_layer1_probe", name: "ToolSearch", input: { query: `select:${PROBE_TOOL}`, max_results: 1 } }] },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "toolu_layer1_probe", content: [{ type: "tool_reference", tool_name: PROBE_TOOL }] }] },
    ],
  };
}

export {
  ADVERT_PHRASE, LEGS, PROBE_TOOL,
  countTokens, hostedBearer, resolveModel, roundTripBody, synthOff, synthOn,
};
