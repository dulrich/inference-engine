import type { RouterSlotRecipe } from "@bench-dashboard/shared";

// The serving recipe the Router view shows per resident model, parsed out of llama-swap's
// per-model `cmd` string (the poller copies it verbatim from `GET /running`).
//
// `apps/api/src/serving/llamaConfig.ts` already does this for `llama/config.yaml`, but its
// helpers (`tokenizeCmd`, `valueAfterToken`, `extractRecipe`) are module-private and its
// output shape is `ServingRecipe` — a different projection, tied to the config file rather
// than to what is actually running. `serving/` is out of this WP's scope, so this is a
// small dedicated parser with the same posture rather than an edit over there: an absent
// flag is `null`, never a guessed default, and the tokenizer is a positional space split
// (this fleet's `cmd:` blocks are space/newline-separated flag pairs, never quoted values).

const FLAG_TOKEN = /^--?[a-zA-Z]/;

function tokenize(cmd: string): string[] {
  return cmd.split(/\s+/).filter((token) => token.length > 0);
}

/** The token after `flag`, or `undefined` when the flag is absent or is followed by
 * another flag (a boolean-style flag carries no value). */
function valueAfter(tokens: string[], flag: string): string | undefined {
  const index = tokens.indexOf(flag);
  if (index < 0) return undefined;
  const next = tokens[index + 1];
  return next !== undefined && !FLAG_TOKEN.test(next) ? next : undefined;
}

function intAfter(tokens: string[], flag: string): number | null {
  const raw = valueAfter(tokens, flag);
  return raw !== undefined && /^\d+$/.test(raw) ? Number.parseInt(raw, 10) : null;
}

/** `cmd` -> the Router view's recipe. A `null`/blank `cmd` (a model `/running` reported
 * without one) yields an all-null recipe INCLUDING `mtp: null` — "unknown", not "no MTP". */
export function parseRouterRecipe(cmd: string | null | undefined): RouterSlotRecipe {
  if (cmd === null || cmd === undefined || cmd.trim().length === 0) {
    return { ctk: null, ctv: null, fa: null, reasoningBudget: null, mtp: null, moeCpuLayers: null };
  }
  const tokens = tokenize(cmd);
  return {
    ctk: valueAfter(tokens, "-ctk") ?? null,
    ctv: valueAfter(tokens, "-ctv") ?? null,
    fa: valueAfter(tokens, "-fa") ?? null,
    reasoningBudget: intAfter(tokens, "--reasoning-budget"),
    // `docs/model-capabilities.md:27`: MTP is "a draft model is configured", by either
    // spelling. Boolean (not null) here because a real `cmd` was parsed.
    mtp: tokens.includes("--spec-type") || tokens.includes("-md"),
    moeCpuLayers: intAfter(tokens, "-ncmoe"),
  };
}
