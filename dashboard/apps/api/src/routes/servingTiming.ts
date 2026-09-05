import type { Hono } from "hono";
import { config } from "../config.ts";
import { listServingTimingRows } from "../db/experiments.ts";
import { readServingConfig } from "../serving/llamaConfig.ts";

// `GET /api/serving/timing` (WP-1, `plans/served-recipe-p50-prefill-and-decode.md` --
// revises WP-3's `plans/bakeoff-timing-records-and-estimates.md` original shape) -- a
// SEPARATE DB-backed aggregate, sibling to `routes/serving.ts` which stays UNTOUCHED
// (untouched-by-contract: that route is a pure `llama/config.yaml` file-parse and must
// never gain a DB dependency, Decisions locked). This route itself DOES gain a file read
// (`config.llamaConfigPath`, via the SAME reader `routes/serving.ts` uses --
// `readServingConfig`/`parseServedAliases`, reused rather than duplicated) so the
// aggregate's served-model resolution always reflects the config as of THIS request; a
// recipe change in `config.yaml` re-keys the view on the next request with nothing baked
// into the DB (Decisions locked).
//
// Response shape: `[{ modelKey, aliases, p50PrefillTokS, p90PrefillTokS, p50DecodeTokS,
// p90DecodeTokS, samplingArm, sampleCount, mtp }]` -- p50 **and p90** for both prefill and
// decode (WP-1 reverses WP-3's original "no p90" call: `bakeoff-timing-records-and-
// estimates.md` WP-3 judged n≈10/record too shallow for a p90 to be honest; this plan
// re-adopts it under an internal-estimate framing, recorded here so it is never read as a
// regression). A served model with zero recipe-matched `timing_round` rounds (the `bge-*`
// embed/rerank aliases are the known case today, and any model whose only measured cells
// are off-recipe) simply does not appear in the array; the web client's join renders the
// literal "—" for that absence, never a guessed zero.
//
// Join rule (Decisions locked, `serving/recipe.ts resolveServedModel`): a row joins by its
// OWN `model_host_path` (normalized `/srv/models/` -> `/models/`, full path never
// basename) against a served model's own `cmd`-derived path when present; otherwise, a
// `v1-timing` row (WP-4's pre-v2 timing-only ingest side door) joins by alias, admitted
// only when the served model's CURRENT recipe is the v1-baseline shape (`{mtp:false,
// moeCpuLayers:null, kvK:"f16", kvV:"f16"}` — the only recipe that harness could ever have
// measured). Only rows whose measured recipe equals the served recipe contribute
// ("`recipeMatch === true`" — "only the currently-served recipe" per Decisions locked);
// everything else stays visible in Runs, never fabricated into a Serving number.
export function registerServingTimingRoutes(app: Hono): void {
  app.get("/api/serving/timing", async (c) => {
    try {
      const servingConfig = await readServingConfig(config.llamaConfigPath);
      return c.json(listServingTimingRows(servingConfig.models));
    } catch (error) {
      return c.json({ error: error instanceof Error ? error.message : String(error) }, 502);
    }
  });
}
