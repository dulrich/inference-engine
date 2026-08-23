import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { useTemporaryDatabase } from "./db/testDatabase.ts";

// `LLAMA_CONFIG_PATH` must be set BEFORE `config.ts` first loads (transitively, via the
// dynamic imports below) — same ordering requirement `APP_DB_PATH` has in
// `testDatabase.ts`'s own header comment. This file never reads the real
// `llama/config.yaml` (brief hard constraint).
process.env.LLAMA_CONFIG_PATH = join(import.meta.dir, "../fixtures/llama-config.yaml");

await useTemporaryDatabase("app-routes");

const { app } = await import("./app.ts");
const { runIngest } = await import("./ingest/run.ts");

const FIXTURES_DIR = join(import.meta.dir, "../fixtures/records");
await runIngest({ recordsDir: FIXTURES_DIR, rebuild: false });

async function getJson<T>(path: string): Promise<{ status: number; body: T }> {
  const response = await app.request(path);
  const body = (await response.json()) as T;
  return { status: response.status, body };
}

describe("GET /health", () => {
  test("probes by body, not just status code: status is exactly \"ok\"", async () => {
    const { status, body } = await getJson<{ status: string; experimentCount: number }>("/health");
    expect(status).toBe(200);
    expect(body.status).toBe("ok");
    expect(body.experimentCount).toBe(2);
  });
});

describe("GET /api/experiments", () => {
  test("lists both ingested v2 experiments, never the skipped v1", async () => {
    const { body } = await getJson<{ id: string }[]>("/api/experiments");
    const ids = body.map((e) => e.id).sort();
    expect(ids).toEqual(["fixture-v2-full", "fixture-v2-smoke-cell"]);
  });
});

describe("GET /api/experiments/:id", () => {
  test("404s for an unknown id", async () => {
    const { status } = await getJson("/api/experiments/does-not-exist");
    expect(status).toBe(404);
  });

  test("returns trials and metrics for the smoke-cell fixture", async () => {
    const { status, body } = await getJson<{ trials: unknown[]; metrics: unknown[]; hasWilsonSummary: boolean }>(
      "/api/experiments/fixture-v2-smoke-cell",
    );
    expect(status).toBe(200);
    expect(body.trials).toHaveLength(3);
    expect(body.metrics).toHaveLength(3); // vram_baseline/loaded/after_teardown
    expect(body.hasWilsonSummary).toBe(false);
  });

  test("hasWilsonSummary is true for the full fixture (non-null wilson_summary)", async () => {
    const { body } = await getJson<{ hasWilsonSummary: boolean }>("/api/experiments/fixture-v2-full");
    expect(body.hasWilsonSummary).toBe(true);
  });
});

describe("GET /api/ladders/*", () => {
  test("returns one row for the full-fixture's hfRepo, with flags parsed from sweep_cell (null here)", async () => {
    const { status, body } = await getJson<{ experimentId: string }[]>("/api/ladders/example-org/Example-Big-GGUF");
    expect(status).toBe(200);
    expect(body).toHaveLength(1);
    expect(body[0]?.experimentId).toBe("fixture-v2-full");
  });

  test("400s when the hfRepo segment is empty", async () => {
    const { status } = await getJson("/api/ladders/");
    expect(status).toBe(400);
  });
});

describe("GET /api/curves/:experimentId", () => {
  test("groups longcontext_probe trials by (task, position) with correct Wilson-scored failure rates", async () => {
    const { status, body } = await getJson<{ points: { positionFraction: number; n: number; k: number }[] }>(
      "/api/curves/fixture-v2-full",
    );
    expect(status).toBe(200);
    expect(body.points).toHaveLength(2);
    const byPosition = new Map(body.points.map((p) => [p.positionFraction, p]));
    expect(byPosition.get(0.1)).toMatchObject({ n: 4, k: 1 });
    expect(byPosition.get(0.45)).toMatchObject({ n: 4, k: 2 });
  });
});

describe("GET /api/serving", () => {
  test("reads the fixture YAML, never the real llama/config.yaml", async () => {
    const { status, body } = await getJson<{ configPath: string; models: { modelKey: string }[] }>("/api/serving");
    expect(status).toBe(200);
    expect(body.configPath).toContain("fixtures/llama-config.yaml");
    expect(body.models.some((m) => m.modelKey === "fixture-chat-q4")).toBe(true);
    expect(body.models.some((m) => m.modelKey === "fixture-unaliased")).toBe(false);
  });
});

describe("GET /api/export/scoreboard-capture", () => {
  test("envelope marks stability \"experimental\" and passes through raw records", async () => {
    const { status, body } = await getJson<{
      stability: string;
      recordCount: number;
      records: { experimentId: string; config: unknown; samples: unknown[] }[];
    }>("/api/export/scoreboard-capture");
    expect(status).toBe(200);
    expect(body.stability).toBe("experimental");
    expect(body.recordCount).toBe(2);
    const smokeCell = body.records.find((r) => r.experimentId === "fixture-v2-smoke-cell");
    expect(smokeCell).toBeDefined();
    expect((smokeCell?.config as { schema_version: number }).schema_version).toBe(2);
    // Reconstructed from `trials`/`metrics` `raw_json`, ordered by `line_index` — matches
    // the original `samples.jsonl`'s 3 `sampling_echo_check` lines exactly (VRAM lives in
    // `config.json`, never `samples.jsonl`, so it is excluded here and reachable instead
    // via `smokeCell.config.vram`).
    expect(smokeCell?.samples).toHaveLength(3);
  });

  test("?id= filters to the requested experiment(s)", async () => {
    const { body } = await getJson<{ recordCount: number; records: { experimentId: string }[] }>(
      "/api/export/scoreboard-capture?id=fixture-v2-full",
    );
    expect(body.recordCount).toBe(1);
    expect(body.records[0]?.experimentId).toBe("fixture-v2-full");
  });
});
