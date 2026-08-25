import type { Hono } from "hono";
import { listServingTimingRows } from "../db/experiments.ts";

// `GET /api/serving/timing` (WP-3, `plans/bakeoff-timing-records-and-estimates.md`) -- a
// SEPARATE DB-backed aggregate, sibling to `routes/serving.ts` which stays UNTOUCHED
// (untouched-by-contract: that route is a pure `llama/config.yaml` file-parse and must
// never gain a DB dependency, Decisions locked). Response shape:
// `[{ alias, p50DecodeTokS, sampleCount }]` -- p50 + sample count only, deliberately no p90
// (n≈10/record makes a p90 dishonest at this depth). An alias with zero qualifying
// `timing_round` samples (the `bge-*` embed/rerank aliases are the known case today) simply
// does not appear in the array; the web client's join renders the literal "—" for that
// absence, never a guessed zero.
//
// Alias join rule (pinned, brief `runs/dispatch/btre-wp-3-brief.md`): the aggregate groups
// by the experiment's `sweep_alias` exactly as stored, EXCEPT for rows whose
// `schema_origin` is `'v1-timing'` (WP-4's pre-v2 timing-only ingest side door) -- those
// have a trailing `-full` suffix stripped before grouping, mapping e.g.
// `gemma-4-12b-qat-full` -> `gemma-4-12b-qat` so a legacy timing record joins onto the same
// served alias a v2 measurement of the same model would use. This stripping NEVER applies
// to `v2` rows. If a stripped legacy alias collides with a v2 alias that also carries
// timing rows, their samples merge (same served model, two measurement eras) -- see
// `listServingTimingRows`/`servingTimingAlias` in `db/experiments.ts` for the
// implementation. (In practice, every `v1-timing` row ingested to date already stores a
// clean alias without the `-full` suffix -- `ingest/record.ts`'s `legacyAlias` reads the
// pre-v2 record's own `config.alias` key, and the suffix lives on the record DIRECTORY
// name, not that field -- so this stripping is a defensive no-op today, pinned verbatim
// per the brief for any legacy dir whose alias does carry it.)
export function registerServingTimingRoutes(app: Hono): void {
  app.get("/api/serving/timing", (c) => {
    return c.json(listServingTimingRows());
  });
}
