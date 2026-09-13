import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { RouterSlotsRecordSchema } from "@bench-dashboard/shared";
import {
  pollOnce,
  pruneOldSlotsFiles,
  slotsFileName,
  writeSlotsRecord,
} from "../../../../scripts/router-slots-poller.mjs";

// The host poller's guard tests. The fixture server is an in-test `Bun.serve({port: 0})`
// on an ephemeral port — NEVER the live llama-swap on :4811 (`dashboard/AGENTS.md`: build
// and test against fixtures only).
//
// The load-bearing assertion in this file is the SPAWN GUARD: `/upstream/<model>/…` is a
// llama-swap proxy path, so asking it about a model that is not resident swaps that model
// into VRAM. The poller must ask only for models `/running` reports `ready`, and the
// fixture server's request log is what proves it.

const READY_SLOTS = [{ id: 0, n_ctx: 131072, speculative: true, is_processing: false }];

let requestLog: string[] = [];

/** An ephemeral-port stand-in for llama-swap. Its request log is what proves the spawn
 * guard: any `/upstream/<model>/slots` path the poller asks for lands in it. */
function startFixtureServer() {
  return Bun.serve({
    port: 0,
    fetch(request: Request) {
      const path = new URL(request.url).pathname;
      requestLog.push(path);
      if (path === "/running") {
        return Response.json({
          running: [
            { model: "ready-model", state: "ready", ttl: 1800, cmd: "/app/llama-server -c 131072 -ctk q8_0" },
            { model: "starting-model", state: "starting", ttl: 1800, cmd: "/app/llama-server -c 8192" },
            { model: "stopping-model", state: "stopping", ttl: 1800, cmd: "/app/llama-server -c 8192" },
            { model: "stopped-model", state: "stopped", ttl: 1800, cmd: "/app/llama-server -c 8192" },
            { model: "boom-model", state: "ready", ttl: 1800, cmd: "/app/llama-server -c 4096" },
          ],
        });
      }
      if (path === "/upstream/ready-model/slots") return Response.json(READY_SLOTS);
      if (path === "/upstream/boom-model/slots") return new Response("upstream exploded", { status: 500 });
      return new Response("not found", { status: 404 });
    },
  });
}

let server: ReturnType<typeof startFixtureServer>;
let baseUrl: string;

beforeAll(() => {
  server = startFixtureServer();
  baseUrl = `http://127.0.0.1:${server.port}`;
});

afterAll(() => {
  server.stop(true);
});

function temporaryDir(): string {
  return mkdtempSync(join(tmpdir(), "bench-dashboard-poller-"));
}

interface PollModel {
  model: string;
  state: string | null;
  ttl: number | null;
  cmd: string | null;
  slots: unknown[] | null;
  error: string | null;
}

/** The record's entry for `name`. Absence is a fixture bug, so it throws rather than
 * returning `undefined` and letting a `?.` silently pass the assertion below. */
function modelEntry(record: { models: unknown[] }, name: string): PollModel {
  const entry = (record.models as PollModel[]).find((candidate) => candidate.model === name);
  if (entry === undefined) throw new Error(`the poller record carries no model "${name}"`);
  return entry;
}

describe("pollOnce — the spawn guard", () => {
  test("a ready model gets exactly one /upstream/<model>/slots call", async () => {
    requestLog = [];
    const record = await pollOnce(fetch, baseUrl, 2000);
    expect(requestLog.filter((path) => path === "/upstream/ready-model/slots")).toHaveLength(1);
    const ready = modelEntry(record, "ready-model");
    expect(ready.slots).toEqual(READY_SLOTS);
    expect(ready.error).toBeNull();
  });

  test("a starting/stopping/stopped model gets ZERO upstream calls but is still reported", async () => {
    requestLog = [];
    const record = await pollOnce(fetch, baseUrl, 2000);
    const upstreamCalls = requestLog.filter((path) => path.startsWith("/upstream/"));
    for (const model of ["starting-model", "stopping-model", "stopped-model"]) {
      expect(upstreamCalls).not.toContain(`/upstream/${model}/slots`);
      const entry = modelEntry(record, model);
      expect(entry.slots).toBeNull();
      expect(entry.error).toBeNull();
    }
    // Only the two `ready` models are ever proxied.
    expect(upstreamCalls.sort()).toEqual(["/upstream/boom-model/slots", "/upstream/ready-model/slots"]);
  });

  test("a model whose /slots 500s is recorded with an error and the tick still completes", async () => {
    const record = await pollOnce(fetch, baseUrl, 2000);
    const boom = modelEntry(record, "boom-model");
    expect(boom.slots).toBeNull();
    expect(boom.error).toContain("HTTP 500");
    expect(record.models).toHaveLength(5);
    expect(record.error).toBeNull();
  });

  test("the record round-trips through the SHARED RouterSlotsRecordSchema (one writer/reader definition)", async () => {
    const record = await pollOnce(fetch, baseUrl, 2000);
    const parsed = RouterSlotsRecordSchema.parse(record);
    expect(parsed.kind).toBe("slots");
    expect(parsed.models).toHaveLength(5);
  });

  test("an absent llama-swap is a recorded error with an empty model list, never a throw", async () => {
    const record = await pollOnce(fetch, "http://127.0.0.1:1", 500);
    expect(() => RouterSlotsRecordSchema.parse(record)).not.toThrow();
    expect(record.models).toEqual([]);
    expect(record.error).toBeTruthy();
  });
});

describe("the poller's file writer", () => {
  test("appends jsonl into slots-YYYY-MM-DD.jsonl at mode 0644", () => {
    const dir = temporaryDir();
    const now = new Date("2026-09-13T04:00:00.000Z");
    const record = { kind: "slots", ts: now.toISOString(), sampledAt: now.toISOString(), error: null, models: [] };
    const file = writeSlotsRecord(dir, record, now);
    expect(file).toBe(join(dir, "slots-2026-09-13.jsonl"));
    writeSlotsRecord(dir, record, now);
    expect(readFileSync(file, "utf8").trimEnd().split("\n")).toHaveLength(2);
    expect(statSync(file).mode & 0o777).toBe(0o644);
    expect(slotsFileName(now)).toBe("slots-2026-09-13.jsonl");
  });

  test("retention removes slots files older than keep-days and nothing else", () => {
    const dir = temporaryDir();
    const now = new Date("2026-09-13T04:00:00.000Z");
    for (const name of ["slots-2026-09-13.jsonl", "slots-2026-09-07.jsonl", "slots-2026-09-01.jsonl"]) {
      writeFileSync(join(dir, name), "{}\n", "utf8");
    }
    writeFileSync(join(dir, "router-2026-09-01.0.jsonl"), "{}\n", "utf8"); // the sink's own file
    expect(pruneOldSlotsFiles(dir, 7, now)).toEqual(["slots-2026-09-01.jsonl"]);
    expect(existsSync(join(dir, "slots-2026-09-07.jsonl"))).toBe(true);
    expect(existsSync(join(dir, "router-2026-09-01.0.jsonl"))).toBe(true);
  });
});
