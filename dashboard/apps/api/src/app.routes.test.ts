import { beforeAll, describe, expect, test } from "bun:test";
import { z } from "zod";
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
const { db } = await import("./db/connection.ts");

const FIXTURES_DIR = join(import.meta.dir, "../fixtures/records");
await runIngest({ recordsDir: FIXTURES_DIR, rebuild: false });

// WP-3 (`plans/served-recipe-p50-prefill-and-decode.md`) fixture pairing for the export's
// `serving` key -- reused verbatim from `routes/servingTiming.test.ts`'s own comment: the
// fixture config's `fixture-chat-q4` model resolves to `-m /models/example-org/
// fixture-chat-gguf/fixture-chat-Q4_K_M.gguf -ctk q4_0 -ctv q4_0`, no MTP, no `-ncmoe` --
// a record whose own `model_host_path`/flags reduce to the identical tuple gets
// `recipeMatch: true` against it. Inserted directly (never through `runIngest`, which only
// scans `FIXTURES_DIR`'s two on-disk records, neither of which carries a `model_host_path`)
// -- same direct-`db.query` pattern `servingTiming.test.ts`/`experiments.test.ts` use for
// resolver fixtures. Deferred to a `beforeAll` scoped to the export describe block below
// (never at this module's top level) so it runs only right before that describe's own
// tests -- after `/health`'s `experimentCount` and `/api/experiments`'s two-id assertions
// have already executed against the two-row DB `runIngest` populated above.
const SERVING_MATCH_EXPERIMENT_ID = "fixture-serving-match";
function insertServingMatchExperiment(): void {
  db.query(
    `
      INSERT INTO experiments (
        id, schema_version, sweep_alias, sampling_arm, sweep_server_flags, model_host_path,
        raw_config_json, raw_prompts_json, raw_completions_json, ingested_at, schema_origin
      ) VALUES (?, 2, ?, ?, ?, ?, ?, '[]', '[]', ?, 'v2')
    `,
  ).run(
    SERVING_MATCH_EXPERIMENT_ID,
    "fixture-serving-match-alias",
    "production",
    JSON.stringify({ "-ctk": "q4_0", "-ctv": "q4_0" }),
    "/models/example-org/fixture-chat-gguf/fixture-chat-Q4_K_M.gguf",
    JSON.stringify({ experiment_id: SERVING_MATCH_EXPERIMENT_ID, schema_version: 2 }),
    new Date().toISOString(),
  );
}

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

describe("GET /api/serving/timing", () => {
  // Wiring/shape smoke test only (audit cycle-1 B2: confirms `registerServingTimingRoutes`
  // is actually reached, before the SPA wildcard) -- `db/experiments.test.ts` covers
  // `listServingTimingRows`/the resolver's arm-preference and percentile math directly.
  // Empty here even though `fixture-v2-full` carries 2 real `timing_round` rows: its
  // `sweep_cell` is `null` (a non-sweep full-CLI record), so it has no `model_host_path`
  // to join on and its `schema_origin` is `v2` (not `v1-timing`) -- nothing to guess, per
  // the null-honest contract this route's header comment documents.
  test("is registered and returns the array wire shape", async () => {
    const { status, body } = await getJson<
      {
        modelKey: string;
        aliases: string[];
        p50PrefillTokS: number | null;
        p90PrefillTokS: number | null;
        p50DecodeTokS: number | null;
        p90DecodeTokS: number | null;
        samplingArm: string | null;
        sampleCount: number;
        mtp: boolean;
      }[]
    >("/api/serving/timing");
    expect(status).toBe(200);
    expect(Array.isArray(body)).toBe(true);
    expect(body).toEqual([]);
  });
});

describe("GET /api/queue", () => {
  // Route-wiring smoke test only — `routes/queue.test.ts` covers `buildQueueListResponse`
  // itself (joined/unjoined/malformed/missing-dir) directly, sidestepping `config.ts`'s
  // shared-across-test-files singleton. This just confirms `registerQueueRoutes` is
  // actually reached (registered before the SPA wildcard) and returns the wire shape.
  test("is registered and returns the {queues, generatedAt} wire shape", async () => {
    const { status, body } = await getJson<{ queues: unknown[]; generatedAt: string }>("/api/queue");
    expect(status).toBe(200);
    expect(Array.isArray(body.queues)).toBe(true);
    expect(typeof body.generatedAt).toBe("string");
  });

  // WP-4: this file never sets `CONTROL_HOST_DIR` (no other test file in this `bun test`
  // process does either, since `routes/queue.test.ts`/`routes/control.test.ts` exercise
  // `buildControlSection`/the record functions directly over explicit dirs, bypassing
  // `config.ts` entirely) — so `config.controlHostDir` stays `undefined` for this whole
  // run, and the wire response must carry an honest `control: null`, never omit the key.
  test("control section is null (CONTROL_HOST_DIR unset) but the key is present on the wire", async () => {
    const { body } = await getJson<{ control: unknown }>("/api/queue");
    expect("control" in body).toBe(true);
    expect(body.control).toBeNull();
  });
});

describe("POST /api/control/*", () => {
  // Route-wiring smoke test only — `routes/control.test.ts` covers
  // `recordSimpleAction`/`recordStartAction`/`recordCancelAction` directly over explicit
  // dirs. `CONTROL_REQUESTS_DIR` is unset for this whole `bun test` process (see the
  // `GET /api/queue` control-null test above), so every one of these must 503 with a
  // body naming the missing config — confirming `registerControlRoutes` is actually
  // reached (registered before the SPA wildcard), not that mutation "works" here.
  test("POST /api/control/arm is registered and 503s with a body naming CONTROL_REQUESTS_DIR", async () => {
    const response = await app.request("/api/control/arm", { method: "POST" });
    expect(response.status).toBe(503);
    const body = (await response.json()) as { error: string };
    expect(body.error).toContain("CONTROL_REQUESTS_DIR");
  });

  test("POST /api/control/disarm is registered and 503s", async () => {
    const response = await app.request("/api/control/disarm", { method: "POST" });
    expect(response.status).toBe(503);
  });

  test("POST /api/control/stop is registered and 503s", async () => {
    const response = await app.request("/api/control/stop", { method: "POST" });
    expect(response.status).toBe(503);
  });

  test("POST /api/control/start is registered and 503s (unconfigured wins over body validation)", async () => {
    const response = await app.request("/api/control/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ queueStem: "ladder-v1", minutes: 120 }),
    });
    expect(response.status).toBe(503);
  });

  test("POST /api/control/cancel is registered and 503s", async () => {
    const response = await app.request("/api/control/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ targetRequestId: "req-20260823T000000Z-0123456789abcdef" }),
    });
    expect(response.status).toBe(503);
  });
});

describe("GET /api/export/scoreboard-capture", () => {
  // WP-3 (`plans/served-recipe-p50-prefill-and-decode.md`): `insertServingMatchExperiment`
  // adds a third DB row (`fixture-serving-match`) whose `model_host_path` resolves to the
  // fixture config's `fixture-chat-q4` served model with a matching recipe. Scoped to a
  // `beforeAll` on THIS describe block only, so it never touches `/health`'s
  // `experimentCount` or `/api/experiments`'s two-id assertions above, which already ran
  // against the two-row DB by the time this fires.
  beforeAll(() => {
    insertServingMatchExperiment();
  });

  interface CaptureRecordBody {
    experimentId: string;
    config: unknown;
    prompts: unknown[];
    completions: unknown[];
    samples: unknown[];
    serving: { modelKey: string; aliases: string[]; recipeMatch: boolean; mtp: boolean; moeCpuLayers: number | null; specType: string | null } | null;
  }

  test("envelope marks captureVersion 1 / stability \"v1\" and passes through raw records", async () => {
    const { status, body } = await getJson<{
      captureVersion: number;
      stability: string;
      recordCount: number;
      records: CaptureRecordBody[];
    }>("/api/export/scoreboard-capture");
    expect(status).toBe(200);
    expect(body.captureVersion).toBe(1);
    expect(body.stability).toBe("v1");
    expect(body.recordCount).toBe(3);
    const smokeCell = body.records.find((r) => r.experimentId === "fixture-v2-smoke-cell");
    expect(smokeCell).toBeDefined();
    expect((smokeCell?.config as { schema_version: number }).schema_version).toBe(2);
    // Reconstructed from `trials`/`metrics` `raw_json`, ordered by `line_index` — matches
    // the original `samples.jsonl`'s 3 `sampling_echo_check` lines exactly (VRAM lives in
    // `config.json`, never `samples.jsonl`, so it is excluded here and reachable instead
    // via `smokeCell.config.vram`). Unchanged from pre-`serving` behavior — proves the
    // verbatim fields stayed byte-for-byte identical.
    expect(smokeCell?.samples).toHaveLength(3);
    // Neither pre-existing fixture carries a `sweep_cell.model_host_path` (`fixture-v2-full`'s
    // `sweep_cell` is null; `fixture-v2-smoke-cell`'s carries no `model_host_path` key) — both
    // resolve to no served model, `null`, never a guessed object.
    const fullRecord = body.records.find((r) => r.experimentId === "fixture-v2-full");
    expect(fullRecord?.serving).toBeNull();
    expect(smokeCell?.serving).toBeNull();
  });

  test("serving: recipeMatch true via the same resolver GET /api/serving/timing uses, for a matched model_host_path", async () => {
    const { body } = await getJson<{ records: CaptureRecordBody[] }>("/api/export/scoreboard-capture?id=fixture-serving-match");
    const record = body.records[0];
    expect(record?.experimentId).toBe("fixture-serving-match");
    expect(record?.serving).toEqual({
      modelKey: "fixture-chat-q4",
      aliases: ["fixture-chat"],
      recipeMatch: true,
      mtp: false,
      moeCpuLayers: null,
      specType: null,
    });
  });

  // F1 (`reviews/2026-09-05-tn-code-review.md`, WP-R1): a matched `model_host_path` whose
  // stored `sweep_server_flags` decodes to a present-but-wrong-typed key must export
  // `serving: null` -- never a guessed/defaulted match -- while every verbatim record field
  // stays exactly as reconciled (this is the F1 acceptance shown through a real caller, not
  // merely through the resolver helper in isolation).
  const SERVING_MALFORMED_FLAGS_EXPERIMENT_ID = "fixture-serving-malformed-flags";
  function insertServingMalformedFlagsExperiment(): void {
    db.query(
      `
        INSERT INTO experiments (
          id, schema_version, sweep_alias, sampling_arm, sweep_server_flags, model_host_path,
          raw_config_json, raw_prompts_json, raw_completions_json, ingested_at, schema_origin
        ) VALUES (?, 2, ?, ?, ?, ?, ?, '[]', '[]', ?, 'v2')
      `,
    ).run(
      SERVING_MALFORMED_FLAGS_EXPERIMENT_ID,
      "fixture-serving-malformed-flags-alias",
      "production",
      JSON.stringify({ "-ctk": 7 }),
      "/models/example-org/fixture-chat-gguf/fixture-chat-Q4_K_M.gguf",
      JSON.stringify({ experiment_id: SERVING_MALFORMED_FLAGS_EXPERIMENT_ID, schema_version: 2 }),
      new Date().toISOString(),
    );
  }

  test("serving: null for a matched model_host_path whose stored flags are malformed -- config/prompts/completions/samples still returned verbatim", async () => {
    insertServingMalformedFlagsExperiment();
    const { body } = await getJson<{ records: CaptureRecordBody[] }>(
      `/api/export/scoreboard-capture?id=${SERVING_MALFORMED_FLAGS_EXPERIMENT_ID}`,
    );
    const record = body.records[0];
    expect(record?.experimentId).toBe(SERVING_MALFORMED_FLAGS_EXPERIMENT_ID);
    expect(record?.serving).toBeNull();
    expect(record?.config).toEqual({ experiment_id: SERVING_MALFORMED_FLAGS_EXPERIMENT_ID, schema_version: 2 });
    expect(record?.prompts).toEqual([]);
    expect(record?.completions).toEqual([]);
    expect(record?.samples).toEqual([]);
  });

  test("?id= filters to the requested experiment(s)", async () => {
    const { body } = await getJson<{ recordCount: number; records: { experimentId: string }[] }>(
      "/api/export/scoreboard-capture?id=fixture-v2-full",
    );
    expect(body.recordCount).toBe(1);
    expect(body.records[0]?.experimentId).toBe("fixture-v2-full");
  });

  // Consumer-compatibility test (audit c1 F4): a local copy of model_scoreboard's own
  // frozen literal check (`local-capture/client.ts`) -- the two envelope literals plus
  // `.passthrough()` on both the envelope and each record -- parses the LIVE route body
  // unmodified. Proves the scoreboard's `.passthrough()` read cannot break on the new
  // additive `serving` key, without importing anything from that repo.
  const ConsumerFrozenCheckSchema = z
    .object({
      captureVersion: z.literal(1),
      stability: z.literal("v1"),
      records: z.array(z.object({ experimentId: z.string() }).passthrough()),
    })
    .passthrough();

  test("consumer-compatibility: model_scoreboard's frozen literal + .passthrough() check parses the live body", async () => {
    const response = await app.request("/api/export/scoreboard-capture");
    const raw: unknown = await response.json();
    expect(() => ConsumerFrozenCheckSchema.parse(raw)).not.toThrow();
  });
});

describe("GET /api/router/{telemetry,slots,sidecar}", () => {
  // The one thing `routes/routerStatus.test.ts` cannot cover: that `app.ts` actually
  // registers all three paths, BEFORE the SPA wildcard. The env vars
  // (`ROUTER_TELEMETRY_DIR` / `CODEX_SIDECAR_LOG_DIR`) are not set in a `bun test` process,
  // so the honest answer here is a typed unavailable payload — 200 with `available: false`,
  // never a 404 (unregistered) and never a 500 (`config.ts`'s absence-is-off convention).
  const RouterStatusEnvelopeSchema = z.union([
    z.object({ available: z.literal(true) }).passthrough(),
    z.object({ available: z.literal(false), reason: z.enum(["disabled", "unreadable", "empty"]) }),
  ]);

  test("all three are registered and answer 200 with a valid router-status envelope", async () => {
    for (const path of ["/api/router/telemetry", "/api/router/slots", "/api/router/sidecar"]) {
      const response = await app.request(path);
      expect(response.status).toBe(200);
      const raw: unknown = await response.json();
      expect(() => RouterStatusEnvelopeSchema.parse(raw)).not.toThrow();
    }
  });
});
