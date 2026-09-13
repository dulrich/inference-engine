import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import {
  RouterSidecarResponseSchema,
  RouterSlotsResponseSchema,
  RouterTelemetryResponseSchema,
} from "@bench-dashboard/shared";
import { buildRouterSidecarResponse } from "../router/sidecarFeed.ts";
import { buildRouterSlotsResponse } from "../router/slotsFeed.ts";
import { buildRouterTelemetryResponse } from "../router/telemetryFeed.ts";
import { STALE_MS_MAX, STALE_MS_MIN, parseStaleMsQuery } from "./routerStatus.ts";

// Route-level contract for `/api/router/*`. Same seam discipline as `routes/queue.test.ts`:
// the builders are exercised with explicit directories, because `config.ts` is a
// module-load singleton shared across the whole `bun test` process and a per-file env
// override would only win for whichever file imported it first. That `app.ts` actually
// registers these three paths is covered in `app.routes.test.ts`, which this cannot do.

const LOGS_DIR = join(import.meta.dir, "../../fixtures/router/logs");
const SIDECAR_DIR = join(import.meta.dir, "../../fixtures/router/sidecar");
const NOW = new Date("2026-09-13T04:00:10.000Z");

describe("/api/router/* with the env unset", () => {
  test("all three routes answer a typed disabled payload, never a 500", async () => {
    const responses = [
      await buildRouterTelemetryResponse(undefined),
      await buildRouterSlotsResponse(undefined),
      await buildRouterSidecarResponse(undefined),
    ];
    for (const response of responses) {
      expect(response).toEqual({ available: false, reason: "disabled" });
    }
    expect(() => RouterTelemetryResponseSchema.parse(responses[0])).not.toThrow();
    expect(() => RouterSlotsResponseSchema.parse(responses[1])).not.toThrow();
    expect(() => RouterSidecarResponseSchema.parse(responses[2])).not.toThrow();
  });

  test("a configured-but-missing mount is unreadable on all three, never a throw", async () => {
    const missing = join(LOGS_DIR, "no-such-mount");
    expect(await buildRouterTelemetryResponse(missing)).toEqual({ available: false, reason: "unreadable" });
    expect(await buildRouterSlotsResponse(missing)).toEqual({ available: false, reason: "unreadable" });
    expect(await buildRouterSidecarResponse(missing)).toEqual({ available: false, reason: "unreadable" });
  });
});

describe("/api/router/* happy paths on the fixtures", () => {
  test("all three parse back through their shared schema; telemetry and slots share one directory", async () => {
    // The poller writes `slots-*.jsonl` BESIDE the router sink, so both routes read
    // `config.routerTelemetryDir`; only the sidecar has its own mount.
    const telemetry = await buildRouterTelemetryResponse(LOGS_DIR, { now: NOW });
    const slots = await buildRouterSlotsResponse(LOGS_DIR, { now: NOW });
    const sidecar = await buildRouterSidecarResponse(SIDECAR_DIR, { now: NOW });
    expect(() => RouterTelemetryResponseSchema.parse(telemetry)).not.toThrow();
    expect(() => RouterSlotsResponseSchema.parse(slots)).not.toThrow();
    expect(() => RouterSidecarResponseSchema.parse(sidecar)).not.toThrow();
    expect([telemetry.available, slots.available, sidecar.available]).toEqual([true, true, true]);
  });
});

describe("?staleMs=", () => {
  test("an in-range value is honoured", () => {
    expect(parseStaleMsQuery("5000")).toBe(5000);
    expect(parseStaleMsQuery(String(STALE_MS_MIN))).toBe(STALE_MS_MIN);
    expect(parseStaleMsQuery(String(STALE_MS_MAX))).toBe(STALE_MS_MAX);
  });

  test("absent, non-numeric or out-of-range is IGNORED (the feed default wins), never a 400", () => {
    expect(parseStaleMsQuery(undefined)).toBeUndefined();
    expect(parseStaleMsQuery("soon")).toBeUndefined();
    expect(parseStaleMsQuery("999")).toBeUndefined();
    expect(parseStaleMsQuery("600001")).toBeUndefined();
  });

  test("an honoured staleMs actually reaches the feed", async () => {
    const wide = await buildRouterTelemetryResponse(LOGS_DIR, { now: NOW, staleMs: parseStaleMsQuery("600000") });
    if (!wide.available) throw new Error("expected available");
    expect(wide.inFlight.map((entry) => entry.reqId)).toContain("req-inflight-abandoned");
  });
});
