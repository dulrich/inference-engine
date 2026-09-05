import { describe, expect, test } from "bun:test";
import { Hono } from "hono";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ServingTimingRowSchema } from "@bench-dashboard/shared";
import { useTemporaryDatabase } from "../db/testDatabase.ts";

// F2 (`reviews/2026-09-05-tn-code-review.md`, WP-R2 `plans/served-recipe-boundary-
// remediation.md`): shows the raw-JSON-to-numeric-sample boundary fixed through the NORMAL
// ingest path (`runIngest`), then the REAL `GET /api/serving/timing` route -- not only via
// direct-SQL fixtures whose `prefill?: number` signature can't even express a malformed raw
// shape (`db/experiments.test.ts` covers those directly; this file is the "record it, don't
// just SQL it in" complement the plan requires).
//
// `LLAMA_CONFIG_PATH` must be set BEFORE `config.ts` first loads (transitively, via
// `servingTiming.ts`'s own import of it) -- same ordering requirement `servingTiming.test.ts`
// documents.
process.env.LLAMA_CONFIG_PATH = join(import.meta.dir, "../../fixtures/llama-config.yaml");

// Own temp DB, own file -- deliberately NOT sharing `servingTiming.test.ts`'s DB (labelled
// `routes-serving-timing`, whose own "shape" test writes rounds under the SAME
// `fixture-chat-q4` served model / production arm this file's record also resolves against).
// A shared DB plus arm preference (`pickArm`: production beats an unlabelled arm) would make
// this file's own assertions depend on bun's test-declaration order across TWO files instead
// of being self-contained -- an isolated DB removes that dependency entirely.
await useTemporaryDatabase("routes-serving-timing-ingest");

const { registerServingTimingRoutes } = await import("./servingTiming.ts");
const { registerExportRoutes } = await import("./export.ts");
const { runIngest } = await import("../ingest/run.ts");

function testApp(): Hono {
  const app = new Hono();
  registerServingTimingRoutes(app);
  return app;
}

function exportApp(): Hono {
  const app = new Hono();
  registerExportRoutes(app);
  return app;
}

// The fixture config's `fixture-chat-q4` model: `-m /models/example-org/fixture-chat-gguf/
// fixture-chat-Q4_K_M.gguf -ctk q4_0 -ctv q4_0`, no MTP, no `-ncmoe` -- a record whose own
// flags reduce to the identical tuple gets `recipeMatch: true` against it (same fixture
// `servingTiming.test.ts` uses).
const FIXTURE_CHAT_MODEL_PATH = "/models/example-org/fixture-chat-gguf/fixture-chat-Q4_K_M.gguf";

interface TimingRowBody {
  modelKey: string;
  aliases: string[];
  p50PrefillTokS: number | null;
  p90PrefillTokS: number | null;
  p50DecodeTokS: number | null;
  p90DecodeTokS: number | null;
  samplingArm: string | null;
  sampleCount: number;
  mtp: boolean;
}

interface CaptureRecordBody {
  experimentId: string;
  samples: Array<Record<string, unknown>>;
}

/** Writes a minimal v2 record dir (`config.json`/`prompts.jsonl`/`completions.jsonl`/
 * `samples.jsonl` -- the exact file set `fixtures/records/fixture-v2-smoke-cell/` uses,
 * `record.test.ts`'s WP-R5 `mkdtempSync` pattern for a throwaway dir never added to the
 * committed fixture corpus) whose three `timing_round` samples carry the malformed raw
 * prefill shapes F2 names: a string, a numeric string, and a JSON boolean. */
function writeMalformedPrefillRecord(): string {
  const recordsParent = mkdtempSync(join(tmpdir(), "bench-dashboard-f2-ingest-"));
  const recordDir = join(recordsParent, "fixture-v2-prefill-boundary");
  mkdirSync(recordDir);

  writeFileSync(
    join(recordDir, "config.json"),
    JSON.stringify({
      schema_version: 2,
      description: "F2 ingest-to-route fixture (WP-R2)",
      timestamp: "2026-09-05T00:00:00+00:00",
      lineage: {},
      wilson_summary: null,
      sweep_cell: {
        cell_id: "fixture-cell-prefill-boundary",
        alias: "fixture-prefill-boundary-alias",
        server_flags: { "-ctk": "q4_0", "-ctv": "q4_0" },
        command: `llama-server -m ${FIXTURE_CHAT_MODEL_PATH} -ctk q4_0 -ctv q4_0`,
        model_host_path: FIXTURE_CHAT_MODEL_PATH,
      },
    }),
  );
  writeFileSync(join(recordDir, "prompts.jsonl"), `${JSON.stringify({ type: "prompt", text: "hello" })}\n`);
  writeFileSync(
    join(recordDir, "completions.jsonl"),
    `${JSON.stringify({ type: "completion", trial: 0, content_text: "hi", finish_reason: "stop" })}\n`,
  );
  writeFileSync(
    join(recordDir, "samples.jsonl"),
    `${[
      JSON.stringify({ type: "timing_round", round_index: 0, server_prompt_eval_tok_s: "fast", observed_decode_tok_s: 10 }),
      JSON.stringify({ type: "timing_round", round_index: 1, server_prompt_eval_tok_s: "123", observed_decode_tok_s: 20 }),
      JSON.stringify({ type: "timing_round", round_index: 2, server_prompt_eval_tok_s: true, observed_decode_tok_s: 30 }),
    ].join("\n")}\n`,
  );

  return recordsParent;
}

describe("GET /api/serving/timing through normal ingest: malformed raw prefill (F2, WP-R2)", () => {
  test('"fast"/"123"/true raw prefill values ingest cleanly, validate against ServingTimingRowSchema with null prefill + numeric decode, and stay verbatim on export', async () => {
    const recordsDir = writeMalformedPrefillRecord();

    const summary = await runIngest({ recordsDir, rebuild: false });
    expect(summary.ingested).toBe(1);
    expect(summary.skippedInvalid).toBe(0);

    const response = await testApp().request("/api/serving/timing");
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(ServingTimingRowSchema.array().safeParse(body).success).toBe(true);

    const row = (body as TimingRowBody[]).find((r) => r.modelKey === "fixture-chat-q4");
    expect(row).toBeDefined();
    expect(row?.p50PrefillTokS).toBeNull();
    expect(row?.p90PrefillTokS).toBeNull();
    expect(typeof row?.p50DecodeTokS).toBe("number");
    expect(typeof row?.p90DecodeTokS).toBe("number");
    expect(row?.sampleCount).toBe(3);

    // Raw fidelity: the malformed prefill values are still exported verbatim
    // ("fast"/"123"/true) -- ingest/export never validate this field (brief hard
    // constraint); only `listServingTimingRows`'s own aggregation boundary rejects them.
    const exportResponse = await exportApp().request("/api/export/scoreboard-capture?id=fixture-v2-prefill-boundary");
    expect(exportResponse.status).toBe(200);
    const exportBody = (await exportResponse.json()) as { records: CaptureRecordBody[] };
    const samples = exportBody.records[0]?.samples ?? [];
    expect(samples.map((s) => s.server_prompt_eval_tok_s)).toEqual(["fast", "123", true]);
  });
});
