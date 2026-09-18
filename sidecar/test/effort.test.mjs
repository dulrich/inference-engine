// effort.test.mjs — the effort precedence rule (plan codex-leg-effort-and-auditor-unpinning, WP-1).
//
// Pure unit tests over `parseModelId` + `resolveEffort`. Precedence, highest first:
//   id suffix (-low|-medium|-high)  ->  output_config.effort  ->  thinking:{type:"disabled"}
//   ->  the MODEL_FAMILIES default.
// The two invariants this file is here to pin: an explicit suffix beats a conflicting request
// field, and NOTHING here is ever an error — every unrecognised shape falls to the family
// default (the no-op invariant inherited from the local leg's patch 0007).
//
// `thinking:{type:"disabled"}` is exercised here and only here: no `disabled` request reaches
// this leg today (titles are short-circuited locally, classifiers/suggestions suppressed), so
// the unit test IS its gate.
import test from "node:test";
import assert from "node:assert/strict";
import { parseModelId, resolveEffort } from "../protocol.mjs";

const resolve = (id, body = {}) => resolveEffort(parseModelId(id), body);

test("an explicit id suffix beats a conflicting output_config.effort", () => {
  assert.deepEqual(resolve("gpt-5.6-sol-low", { output_config: { effort: "high" } }),
    { effort: "low", effortSource: "suffix" });
  assert.deepEqual(resolve("gpt-5.6-luna-high", { output_config: { effort: "low" }, thinking: { type: "disabled" } }),
    { effort: "high", effortSource: "suffix" });
  // a persona-prefixed id is the same id
  assert.deepEqual(resolve("codex-b/gpt-5.6-sol-low", { output_config: { effort: "max" } }),
    { effort: "low", effortSource: "suffix" });
});

test("a bare id takes the request's output_config.effort, with max/xhigh folded onto high", () => {
  for (const [want, effort] of [["low", "low"], ["medium", "medium"], ["high", "high"], ["max", "high"], ["xhigh", "high"]]) {
    assert.deepEqual(resolve("gpt-5.6-sol", { output_config: { effort: want } }),
      { effort, effortSource: "request" }, `output_config.effort ${want}`);
  }
});

test("thinking:{type:\"disabled\"} floors a bare id at low (the app-server has no off level)", () => {
  assert.deepEqual(resolve("gpt-5.6-sol", { thinking: { type: "disabled" } }),
    { effort: "low", effortSource: "request" });
  // but it ranks BELOW an explicit level — the deliberate divergence from patch 0007
  assert.deepEqual(resolve("gpt-5.6-sol", { output_config: { effort: "high" }, thinking: { type: "disabled" } }),
    { effort: "high", effortSource: "request" });
});

test("a bare id with no effort field keeps today's family default", () => {
  assert.deepEqual(resolve("gpt-5.6-luna", {}), { effort: "low", effortSource: "default" });
  assert.deepEqual(resolve("gpt-5.6-sol", {}), { effort: "medium", effortSource: "default" });
  assert.deepEqual(resolve("gpt-6-astra", {}), { effort: "medium", effortSource: "default" });
  // what the harness actually sends today alongside output_config (measured 2026-09-13)
  assert.deepEqual(resolve("gpt-5.6-sol", { thinking: { type: "adaptive", display: "omitted" } }),
    { effort: "medium", effortSource: "default" });
});

test("an unrecognised effort value falls to the default and is never an error", () => {
  for (const bad of ["ultra", "none", "", "HIGH", 3, null, {}]) {
    assert.deepEqual(resolve("gpt-5.6-sol", { output_config: { effort: bad } }),
      { effort: "medium", effortSource: "default" }, `output_config.effort ${JSON.stringify(bad)}`);
  }
  assert.deepEqual(resolve("gpt-5.6-sol", { output_config: null }), { effort: "medium", effortSource: "default" });
});

test("thinking:{type:\"enabled\", budget_tokens} falls to the default — 0007's budget rung is omitted here", () => {
  assert.deepEqual(resolve("gpt-5.6-sol", { thinking: { type: "enabled", budget_tokens: 512 } }),
    { effort: "medium", effortSource: "default" });
  assert.deepEqual(resolve("gpt-5.6-luna", { thinking: { type: "enabled", budget_tokens: 24000 } }),
    { effort: "low", effortSource: "default" });
  // …but an explicit level still wins over it
  assert.deepEqual(resolve("gpt-5.6-luna", { thinking: { type: "enabled", budget_tokens: 512 }, output_config: { effort: "high" } }),
    { effort: "high", effortSource: "request" });
});
