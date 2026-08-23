import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { useTemporaryDatabase } from "../db/testDatabase.ts";

// Module-body call, before any other import reaching `db/connection.ts` (the file's own
// isolation seam — see `testDatabase.ts`'s header comment for why this must happen here,
// not inside a `test()`/`beforeEach()`).
await useTemporaryDatabase("ingest-run");

const { db } = await import("../db/connection.ts");
const { experimentCount, listExperimentSummaries, listTrials } = await import("../db/experiments.ts");
const { runIngest } = await import("./run.ts");

const FIXTURES_DIR = join(import.meta.dir, "../../fixtures/records");

function contentSnapshot() {
  // Every content column EXCEPT the autoincrement `id` — that column is a storage detail,
  // not part of a row's identity, and is expected to differ across a delete-then-insert
  // re-ingest even when the row's own content is byte-identical.
  const experiments = db
    .query(
      `SELECT id, schema_version, description, timestamp, hf_repo, hf_revision, filename, file_size, sha256,
              llama_cpp_build, sweep_cell_id, sweep_alias, sweep_server_flags, sweep_command,
              wilson_summary_json, raw_config_json, raw_prompts_json, raw_completions_json
       FROM experiments ORDER BY id ASC`,
    )
    .all();
  const trials = db
    .query(
      `SELECT experiment_id, line_index, type, trial, sampling_arm, temperature, top_p, top_k, sampler_seed,
              haystack_seed, corpus_manifest_id, haystack_sha256, classifier_verdict, classifier_shape,
              prompt_tokens, completion_tokens, finish_reason, position_fraction, task, cell_id, correct, raw_json
       FROM trials ORDER BY experiment_id ASC, line_index ASC`,
    )
    .all();
  const metrics = db
    .query(
      `SELECT experiment_id, line_index, metric_type, round_type, round_index, bytes, first_generation_token_ms,
              observed_decode_tok_s, prompt_tokens, cached_tokens, cache_hit_ratio, cache_assertion_passed, raw_json
       FROM metrics ORDER BY experiment_id ASC, id ASC`,
    )
    .all();
  const summaries = db.query(`SELECT * FROM summaries ORDER BY experiment_id ASC`).all();
  return { experiments, trials, metrics, summaries };
}

describe("runIngest", () => {
  test("scans the fixture tree: 2 v2 ingested, 1 v1 skipped, 0 invalid", async () => {
    const summary = await runIngest({ recordsDir: FIXTURES_DIR, rebuild: false });
    expect(summary.scanned).toBe(3);
    expect(summary.ingested).toBe(2);
    expect(summary.skippedV1).toBe(1);
    expect(summary.skippedInvalid).toBe(0);
    expect(experimentCount()).toBe(2);
  });

  test("v1 records never reach the experiments table", () => {
    const ids = listExperimentSummaries().map((e) => e.id);
    expect(ids).not.toContain("fixture-v1-legacy");
    expect(ids).toContain("fixture-v2-smoke-cell");
    expect(ids).toContain("fixture-v2-full");
  });

  test("re-ingesting the same tree twice produces identical content tables", async () => {
    const before = contentSnapshot();
    await runIngest({ recordsDir: FIXTURES_DIR, rebuild: false });
    const after = contentSnapshot();
    expect(after).toEqual(before);
    expect(experimentCount()).toBe(2); // no duplicate rows from the second pass
  });

  test("--rebuild drops and replays: content tables match a normal re-ingest", async () => {
    const before = contentSnapshot();
    const summary = await runIngest({ recordsDir: FIXTURES_DIR, rebuild: true });
    expect(summary.rebuilt).toBe(true);
    expect(summary.ingested).toBe(2);
    const after = contentSnapshot();
    expect(after).toEqual(before);
  });

  test("unknown v2 sample types survive ingest into the trials table", () => {
    const trials = listTrials("fixture-v2-full");
    const unknown = trials.find((t) => t.type === "future_probe_type");
    expect(unknown).toBeDefined();
  });
});
