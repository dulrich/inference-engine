import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  RouterSidecarResponseSchema,
  RouterSlotsResponseSchema,
  RouterTelemetryResponseSchema,
} from "@bench-dashboard/shared";
import { parseRouterRecipe } from "./cmdRecipe.ts";
import { buildRouterSidecarResponse } from "./sidecarFeed.ts";
import { buildRouterSlotsResponse } from "./slotsFeed.ts";
import { buildRouterTelemetryResponse } from "./telemetryFeed.ts";

// Feed readers over the committed fixtures under `apps/api/fixtures/router/`. Every feed is
// driven through its exported builder with an explicit directory — never `config.*`, which
// is a module-load singleton shared across the whole `bun test` process
// (`routes/queue.test.ts`'s own header makes the same point).

const LOGS_DIR = join(import.meta.dir, "../../fixtures/router/logs");
const SIDECAR_DIR = join(import.meta.dir, "../../fixtures/router/sidecar");

// The fixtures are stamped 2026-09-13T04:00:0x; this clock is 10 s after the newest
// progress line, so the live in-flight stream is 1 s old and the abandoned one is 70 s old.
const NOW = new Date("2026-09-13T04:00:10.000Z");

function emptyDir(): string {
  return mkdtempSync(join(tmpdir(), "bench-dashboard-router-empty-"));
}

describe("buildRouterTelemetryResponse", () => {
  test("unset / unreadable / empty all yield a typed unavailable payload, never a throw", async () => {
    expect(await buildRouterTelemetryResponse(undefined)).toEqual({ available: false, reason: "disabled" });
    expect(await buildRouterTelemetryResponse(join(emptyDir(), "no-such-mount"))).toEqual({
      available: false,
      reason: "unreadable",
    });
    expect(await buildRouterTelemetryResponse(emptyDir())).toEqual({ available: false, reason: "empty" });
  });

  test("selects the highest NUMERIC index for the newest day, not the lexicographically last file", async () => {
    const response = await buildRouterTelemetryResponse(LOGS_DIR, { now: NOW });
    // `router-2026-09-13.2.jsonl` sorts after `.10.jsonl` as a string; `.10` is the file the
    // sink is appending to (patch 0006 `highestIndexForDay`), and `2026-09-12.0` is an older day.
    expect(response.available).toBe(true);
    if (!response.available) return;
    expect(response.file).toBe("router-2026-09-13.10.jsonl");
    expect(response.recent.map((record) => record.reqId)).not.toContain("rotated-out-0002");
    expect(response.recent.map((record) => record.reqId)).not.toContain("yesterday-0001");
  });

  test("happy path parses back through the shared schema, malformed line skipped not fatal", async () => {
    const response = await buildRouterTelemetryResponse(LOGS_DIR, { now: NOW });
    expect(() => RouterTelemetryResponseSchema.parse(response)).not.toThrow();
    expect(response.available).toBe(true);
    if (!response.available) return;
    // Five well-formed terminal records; the sixth line is a truncated fragment.
    expect(response.recent).toHaveLength(5);
    expect(response.recent.map((record) => record.reqId)).toEqual([
      "req-ok-local-1",
      "req-ok-local-2",
      "req-ok-anthropic-1",
      "req-err-anthropic-1",
      "req-err-unresolved-1",
    ]);
    expect(response.windowBytes).toBe(1024 * 1024);
  });

  test("legs roll up per leg with the null (pre-resolution) bucket kept separate", async () => {
    const response = await buildRouterTelemetryResponse(LOGS_DIR, { now: NOW });
    if (!response.available) throw new Error("expected available");
    const byLeg = new Map(response.legs.map((leg) => [leg.leg, leg]));
    expect(byLeg.get("anthropic")).toEqual({ leg: "anthropic", requests: 2, errors: 1, p50TtftMs: 1200 });
    expect(byLeg.get("claude-local")).toEqual({ leg: "claude-local", requests: 2, errors: 0, p50TtftMs: 630 });
    // status 400 with an error envelope, and no leg at all.
    expect(byLeg.get(null)).toEqual({ leg: null, requests: 1, errors: 1, p50TtftMs: null });
  });

  test("inFlight keeps the live progress-only stream, drops the terminated and the aged-out ones", async () => {
    const response = await buildRouterTelemetryResponse(LOGS_DIR, { now: NOW });
    if (!response.available) throw new Error("expected available");
    expect(response.inFlight).toHaveLength(1);
    const live = response.inFlight[0];
    expect(live?.reqId).toBe("req-inflight-live");
    expect(live?.phase).toBe("decode");
    expect(live?.responseTokens).toBe(60);
    expect(live?.reasoningTokens).toBe(90);
    // Last two samples: (60+90) - (40+60) = 50 tokens over 1000 ms.
    expect(live?.tokPerSec).toBe(50);
    const ids = response.inFlight.map((entry) => entry.reqId);
    expect(ids).not.toContain("req-inflight-abandoned"); // 70 s old, staleMs 15000
    expect(ids).not.toContain("req-ok-local-1"); // has a terminal record in the window
  });

  test("a widened staleMs brings the abandoned stream back (the cutoff is the only reason it was dropped)", async () => {
    const response = await buildRouterTelemetryResponse(LOGS_DIR, { now: NOW, staleMs: 600_000 });
    if (!response.available) throw new Error("expected available");
    const abandoned = response.inFlight.find((entry) => entry.reqId === "req-inflight-abandoned");
    expect(abandoned).toBeDefined();
    expect(abandoned?.tokPerSec).toBeNull(); // a single sample cannot give a rate
  });

  test("maxRecent keeps the NEWEST records and legs stay consistent with them", async () => {
    const response = await buildRouterTelemetryResponse(LOGS_DIR, { now: NOW, maxRecent: 2 });
    if (!response.available) throw new Error("expected available");
    expect(response.recent.map((record) => record.reqId)).toEqual(["req-err-anthropic-1", "req-err-unresolved-1"]);
    expect(response.legs.reduce((total, leg) => total + leg.requests, 0)).toBe(2);
  });
});

describe("parseRouterRecipe", () => {
  test("pulls the flag subset out of a real llama-swap cmd; absent flags are null", () => {
    expect(parseRouterRecipe("/app/llama-server -c 131072 -ctk q8_0 -ctv q8_0 -fa on -ncmoe 12 --spec-type mtp")).toEqual(
      { ctk: "q8_0", ctv: "q8_0", fa: "on", reasoningBudget: null, mtp: true, moeCpuLayers: 12 },
    );
  });

  test("a cmd with no draft model is mtp:false; no cmd at all is mtp:null (unknown)", () => {
    expect(parseRouterRecipe("/app/llama-server -c 8192 --embeddings").mtp).toBe(false);
    expect(parseRouterRecipe(null)).toEqual({
      ctk: null,
      ctv: null,
      fa: null,
      reasoningBudget: null,
      mtp: null,
      moeCpuLayers: null,
    });
  });
});

describe("buildRouterSlotsResponse", () => {
  test("unset / unreadable / empty all yield a typed unavailable payload", async () => {
    expect(await buildRouterSlotsResponse(undefined)).toEqual({ available: false, reason: "disabled" });
    expect(await buildRouterSlotsResponse(join(emptyDir(), "no-such-mount"))).toEqual({
      available: false,
      reason: "unreadable",
    });
    expect(await buildRouterSlotsResponse(emptyDir())).toEqual({ available: false, reason: "empty" });
  });

  test("the NEWEST record wins and parses back through the shared schema", async () => {
    const response = await buildRouterSlotsResponse(LOGS_DIR, { now: NOW });
    expect(() => RouterSlotsResponseSchema.parse(response)).not.toThrow();
    if (!response.available) throw new Error("expected available");
    expect(response.sampledAt).toBe("2026-09-13T04:00:05.000Z"); // not the 03:59:55 record
    expect(response.ageMs).toBe(5000);
    expect(response.stale).toBe(false);
    expect(response.models.map((model) => model.model)).toEqual([
      "Qwen3.8-27B-UD-IQ3_S",
      "nemotron-4b",
      "bge-m3-f16",
    ]);
  });

  test("projects n_ctx, the prefix-cache ratio and the recipe off the resident model", async () => {
    const response = await buildRouterSlotsResponse(LOGS_DIR, { now: NOW });
    if (!response.available) throw new Error("expected available");
    const qwen = response.models[0];
    expect(qwen?.nCtx).toBe(131072);
    expect(qwen?.slots[0]).toEqual({
      id: 0,
      nCtx: 131072,
      isProcessing: true,
      promptTokens: 2000,
      promptTokensCache: 1500,
      cacheHitRatio: 0.75,
    });
    expect(qwen?.recipe).toEqual({
      ctk: "q8_0",
      ctv: "q8_0",
      fa: "on",
      reasoningBudget: 8192,
      mtp: true,
      moeCpuLayers: 12,
    });
  });

  test("a non-ready model is reported with no slots (the poller never proxied it) and an errored one carries its error", async () => {
    const response = await buildRouterSlotsResponse(LOGS_DIR, { now: NOW });
    if (!response.available) throw new Error("expected available");
    const starting = response.models[1];
    expect(starting?.state).toBe("starting");
    expect(starting?.slots).toEqual([]);
    expect(starting?.error).toBeNull();
    const errored = response.models[2];
    expect(errored?.state).toBe("ready");
    expect(errored?.slots).toEqual([]);
    expect(errored?.error).toContain("HTTP 500");
  });

  test("stale is a reader verdict from sampledAt, not a field the poller writes", async () => {
    const later = new Date("2026-09-13T04:01:00.000Z"); // 55 s after the newest sample
    const response = await buildRouterSlotsResponse(LOGS_DIR, { now: later });
    if (!response.available) throw new Error("expected available");
    expect(response.ageMs).toBe(55_000);
    expect(response.stale).toBe(true);
    const widened = await buildRouterSlotsResponse(LOGS_DIR, { now: later, staleMs: 120_000 });
    if (!widened.available) throw new Error("expected available");
    expect(widened.stale).toBe(false);
  });
});

describe("buildRouterSidecarResponse", () => {
  test("unset / unreadable / empty all yield a typed unavailable payload", async () => {
    expect(await buildRouterSidecarResponse(undefined)).toEqual({ available: false, reason: "disabled" });
    expect(await buildRouterSidecarResponse(join(emptyDir(), "no-such-mount"))).toEqual({
      available: false,
      reason: "unreadable",
    });
    expect(await buildRouterSidecarResponse(emptyDir())).toEqual({ available: false, reason: "empty" });
  });

  test("counts an OPEN kind set and parses back through the shared schema", async () => {
    const response = await buildRouterSidecarResponse(SIDECAR_DIR, { now: NOW });
    expect(() => RouterSidecarResponseSchema.parse(response)).not.toThrow();
    if (!response.available) throw new Error("expected available");
    expect(response.file).toBe("codex-sidecar-2026-09-13.jsonl");
    expect(response.byKind).toEqual({
      listen: 1,
      "config-verified": 1,
      "http-start": 1,
      http: 1,
      "http-error": 1,
      "orphan-tool-result": 1,
      "child-stderr": 1,
      count_tokens: 1,
    });
  });

  test("personas carry the latest threadId/model and the latest kind of any record", async () => {
    const response = await buildRouterSidecarResponse(SIDECAR_DIR, { now: NOW });
    if (!response.available) throw new Error("expected available");
    expect(response.personas).toEqual([
      {
        persona: "codex-b",
        threadId: "01a098ec-6ba9-7e41-87f6-c0e802f57acd",
        model: "gpt-5.6-sol",
        lastSeenTs: "2026-09-13T03:59:45.000Z",
        lastKind: "orphan-tool-result",
      },
      {
        persona: "codex-a",
        threadId: null,
        model: null,
        lastSeenTs: "2026-09-13T04:00:00.000Z",
        lastKind: "count_tokens",
      },
    ]);
  });

  test("recentErrors spans http-error, child-stderr and orphan-tool-result, with the text fallback", async () => {
    const response = await buildRouterSidecarResponse(SIDECAR_DIR, { now: NOW });
    if (!response.available) throw new Error("expected available");
    expect(response.recentErrors).toEqual([
      {
        ts: "2026-09-13T03:59:40.000Z",
        persona: "codex-b",
        kind: "http-error",
        status: 409,
        message: "superseded by a newer request on this session",
      },
      {
        ts: "2026-09-13T03:59:45.000Z",
        persona: "codex-b",
        kind: "orphan-tool-result",
        status: null,
        message: null,
      },
      {
        ts: "2026-09-13T03:59:50.000Z",
        persona: "codex-a",
        kind: "child-stderr",
        status: null,
        message: "warning: thread pool saturated",
      },
    ]);

    // …and the list is capped to the newest N.
    const capped = await buildRouterSidecarResponse(SIDECAR_DIR, { now: NOW, maxRecentErrors: 1 });
    if (!capped.available) throw new Error("expected available");
    expect(capped.recentErrors).toHaveLength(1);
    expect(capped.recentErrors[0]?.kind).toBe("child-stderr");
  });
});
