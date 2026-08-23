import { unlinkSync } from "node:fs";
import { join } from "node:path";

// `bun run smoke` (root) -> `bun run --filter '@bench-dashboard/api' smoke` -> this file.
// Boots the API against a temp DB, ingests a fixture v2 record, probes `/health` and one
// data route BY RESPONSE BODY (root `AGENTS.md` Quirks — never by status code alone), and
// exits nonzero on any failure (an uncaught throw propagates; Bun exits non-zero).
// In-process (`app.request`, no real socket bind) — same pattern as the model_scoreboard
// exemplar's own `smoke.ts`.

const dbPath = join("/tmp", `bench-dashboard-smoke-${process.pid}-${Date.now()}.db`);
process.env.APP_DB_PATH = dbPath; // set BEFORE any import that reaches db/connection.ts

const { app } = await import("./app.ts");
const { runIngest } = await import("./ingest/run.ts");

const requestJson = async <T>(path: string): Promise<{ status: number; body: T }> => {
  const response = await app.request(path);
  const body = (await response.json()) as T;
  return { status: response.status, body };
};

const assertStatus = (label: string, actual: number, expected: number) => {
  if (actual !== expected) throw new Error(`${label}: expected HTTP ${expected}, got ${actual}`);
};

try {
  const fixturesDir = join(import.meta.dir, "../fixtures/records");
  const summary = await runIngest({ recordsDir: fixturesDir, rebuild: false });
  if (summary.ingested !== 2) {
    throw new Error(`expected 2 ingested fixture v2 records, got ${summary.ingested} (summary: ${JSON.stringify(summary)})`);
  }
  if (summary.skippedV1 !== 1) {
    throw new Error(`expected 1 skipped v1 fixture record, got ${summary.skippedV1}`);
  }

  const health = await requestJson<{ status: string; experimentCount: number }>("/health");
  assertStatus("health", health.status, 200);
  if (health.body.status !== "ok") throw new Error(`health did not return status "ok": ${JSON.stringify(health.body)}`);
  if (health.body.experimentCount !== 2) {
    throw new Error(`health experimentCount: expected 2, got ${health.body.experimentCount}`);
  }

  const experiments = await requestJson<{ id: string }[]>("/api/experiments");
  assertStatus("experiments list", experiments.status, 200);
  if (!experiments.body.some((e) => e.id === "fixture-v2-smoke-cell")) {
    throw new Error("ingested fixture experiment missing from /api/experiments");
  }

  const detail = await requestJson<{ id: string; trialCount: number }>("/api/experiments/fixture-v2-smoke-cell");
  assertStatus("experiment detail", detail.status, 200);
  if (detail.body.trialCount !== 3) {
    throw new Error(`fixture-v2-smoke-cell trialCount: expected 3, got ${detail.body.trialCount}`);
  }

  console.log(JSON.stringify({ ok: true, checks: ["ingest", "health", "experiments", "experiment-detail"] }));
} finally {
  for (const suffix of ["", "-wal", "-shm"]) {
    try {
      unlinkSync(`${dbPath}${suffix}`);
    } catch {
      // SQLite may not create every sidecar file.
    }
  }
}
