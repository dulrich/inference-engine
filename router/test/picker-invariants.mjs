// Shared picker invariants: the rules that hold for ANY valid visibility choice, asserted as shape
// over whatever a catalog lists — never which ids or how many (operator data; root gate 6). One
// copy, called by model-picker-lineup.test.mjs (router-shaped fixtures) and by
// conditional/model-picker-real-catalog.test.mjs (the deployed config and catalog). Concrete
// fixture-presence assertions stay in the lineup test; this module names no model id except the
// two tombstoned (retired) ids, which are the gate itself.

import assert from "node:assert/strict";

// rows: parsed `model-picker-lineup.mjs` output; config: the static gateway config; catalog: the
// model-catalog-v1 document; personas: the codex provider names whose suffix/family shape to check.
export function assertPickerInvariants(rows, config, catalog, personas) {
  const models = rows.map((r) => r.model);
  assert.equal(new Set(models).size, models.length, "no duplicate picker rows");
  assert.equal(models.filter((m) => m.startsWith("anthropic/")).length, 0);
  // Derive the expected set from the catalog itself: an explicit `cc` tag is a row on every
  // non-anthropic provider the static config serves; an explicit tag list without `cc` never is.
  // (An absent `consumers` — legal off claude-local — is covered by the fixture tests.)
  const served = new Set((config.providers ?? []).map((p) => p.name).filter((n) => n !== "anthropic"));
  const expected = new Set();
  const excluded = new Set();
  for (const [name, provider] of Object.entries(catalog.providers)) {
    if (!served.has(name)) continue;
    for (const e of provider.models ?? []) {
      if (!Array.isArray(e.consumers)) continue;
      (e.consumers.includes("cc") ? expected : excluded).add(`${name}/${e.id}`);
    }
  }
  for (const m of expected) assert.ok(models.includes(m), `${m} is cc-tagged in the catalog but is not a picker row`);
  for (const m of excluded) assert.equal(models.includes(m), false, `${m} is not cc-tagged but is a picker row`);
  for (const row of rows) {
    assert.ok(row.description.length < 90, `description too long: ${row.description}`);
    // A bare codex id (whichever are visible) takes its effort from the harness.
    if (/^codex-[^/]+\/(?!.*-(low|medium|high)$)/.test(row.model)) assert.match(row.description, /effort via the harness/);
  }
  // Alias contract (append-only), asserted as SHAPE: every persona's provider exists with >=1 id;
  // every codex `-low|-medium|-high` suffix id has its bare family id in the same persona (the
  // picker's family rule: the script labels a suffix id from its bare family and renders only the
  // bare row); every claude-local `<base>-(off|low|medium|high)` id has `<base>` as a claude-local
  // id. An id dropped from models[] 400s at model_resolution. /v1/models on the live router is
  // smoke-router.sh check 1's job.
  const idsOf = (name) => catalog.providers[name].models.map((e) => e.id);
  assert.ok(personas.length > 0, "set ROUTER_FLEET_PERSONAS to the codex providers to check");
  for (const persona of personas) {
    assert.ok(catalog.providers[persona], `persona ${persona} is not a provider in the catalog`);
    const ids = idsOf(persona);
    assert.ok(ids.length > 0, `${persona} has no ids in models[]`);
    for (const id of ids) {
      const m = /^(.+)-(low|medium|high)$/.exec(id);
      if (m) assert.ok(ids.includes(m[1]), `${persona}/${id} is a suffix id but its family ${persona}/${m[1]} is not in models[]`);
    }
  }
  const localIds = idsOf("claude-local");
  for (const id of localIds) {
    const m = /^(.+)-(off|low|medium|high)$/.exec(id);
    if (m) assert.ok(localIds.includes(m[1]), `claude-local/${id} is an effort id but its base claude-local/${m[1]} is not in models[]`);
  }
  // TOMBSTONE GATE (router contract, user decision 2026-09-14): these two ids were retired from
  // serving. Assert they are gone from the CATALOG entirely, on both legs, so a future edit cannot
  // quietly re-add one. The names stay reserved forever; re-adding either string for different
  // weights is the append-only violation this gate exists to catch.
  for (const legName of ["openrouter", "or-anthropic"]) {
    for (const id of ["z-ai/glm-5.2:free", "z-ai/glm-5.3"]) {
      assert.equal(idsOf(legName).includes(id), false, `retired id ${legName}/${id} must stay removed`);
    }
  }
  // The conversion leg keeps a resolvable id: an empty models[] is a WILDCARD on a credentialed
  // OR provider.
  assert.ok(idsOf("openrouter").length > 0, "openrouter models[] must never be empty (wildcard)");
}
