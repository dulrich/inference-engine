import { describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ControlAcceptedResponse } from "@bench-dashboard/shared";
import {
  type ControlRequestFileV1,
  generateRequestId,
  recordCancelAction,
  recordSimpleAction,
  recordStartAction,
  writeControlRequestExclusive,
} from "./control.ts";

// Tests `recordSimpleAction`/`recordStartAction`/`recordCancelAction`/
// `writeControlRequestExclusive` directly over an explicit `requestsDir` — same seam
// pattern as `queue.test.ts`'s `buildQueueListResponse`, for the same reason: `config.ts`
// is a singleton shared across the whole `bun test` process.

function makeRequestsDir(prefix: string): string {
  return mkdtempSync(join(tmpdir(), `bench-dashboard-control-requests-${prefix}-`));
}

function readRequestFile(dir: string, requestId: string): ControlRequestFileV1 {
  return JSON.parse(readFileSync(join(dir, `${requestId}.json`), "utf8")) as ControlRequestFileV1;
}

function isAccepted(body: unknown): body is ControlAcceptedResponse {
  return typeof body === "object" && body !== null && (body as { accepted?: unknown }).accepted === true;
}

const REQUEST_ID_GRAMMAR = /^req-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$/;

describe("generateRequestId", () => {
  test("matches the pinned grammar req-<UTCcompact>-<16 hex>", () => {
    expect(generateRequestId()).toMatch(REQUEST_ID_GRAMMAR);
  });
});

describe("recordSimpleAction: arm/disarm/stop", () => {
  test("arm: writes a schema-valid control-request-v1 file and returns {requestId, accepted: true}", () => {
    const dir = makeRequestsDir("arm");
    const result = recordSimpleAction(dir, "arm");
    expect(result.status).toBe(200);
    if (!isAccepted(result.body)) throw new Error("expected an accepted response");
    expect(result.body.requestId).toMatch(REQUEST_ID_GRAMMAR);
    expect(result.body.message).toBeUndefined();

    const file = readRequestFile(dir, result.body.requestId);
    expect(file).toMatchObject({ schema: "control-request-v1", action: "arm", request_id: result.body.requestId });
    expect(typeof file.requested_at).toBe("string");
  });

  test("disarm: writes a schema-valid file with action:disarm", () => {
    const dir = makeRequestsDir("disarm");
    const result = recordSimpleAction(dir, "disarm");
    if (!isAccepted(result.body)) throw new Error("expected an accepted response");
    const file = readRequestFile(dir, result.body.requestId);
    expect(file.action).toBe("disarm");
  });

  test("stop: writes action:stop and the response message carries the delivered-vs-completed caveat", () => {
    const dir = makeRequestsDir("stop");
    const result = recordSimpleAction(dir, "stop");
    if (!isAccepted(result.body)) throw new Error("expected an accepted response");
    expect(result.body.message).toContain("pause-after-current-cell");
    expect(result.body.message).toContain("no-op");
    const file = readRequestFile(dir, result.body.requestId);
    expect(file.action).toBe("stop");
  });

  test("503 with a body naming the missing config when requestsDir is undefined", () => {
    const result = recordSimpleAction(undefined, "arm");
    expect(result.status).toBe(503);
    expect(JSON.stringify(result.body)).toContain("CONTROL_REQUESTS_DIR");
  });
});

describe("recordStartAction", () => {
  test("valid body: writes queue_stem/minutes onto the request file", () => {
    const dir = makeRequestsDir("start-valid");
    const result = recordStartAction(dir, { queueStem: "ladder-v1", minutes: 120 });
    expect(result.status).toBe(200);
    if (!isAccepted(result.body)) throw new Error("expected an accepted response");
    const file = readRequestFile(dir, result.body.requestId);
    expect(file).toMatchObject({ action: "start", queue_stem: "ladder-v1", minutes: 120 });
  });

  test("400 on an invalid queueStem (uppercase not allowed)", () => {
    const dir = makeRequestsDir("start-bad-stem");
    const result = recordStartAction(dir, { queueStem: "Ladder-V1", minutes: 60 });
    expect(result.status).toBe(400);
    expect(existsSync(dir)).toBe(true);
  });

  test("400 on minutes below the [15, 480] bound", () => {
    const dir = makeRequestsDir("start-bad-minutes-low");
    const result = recordStartAction(dir, { queueStem: "ladder-v1", minutes: 5 });
    expect(result.status).toBe(400);
  });

  test("400 on minutes above the [15, 480] bound", () => {
    const dir = makeRequestsDir("start-bad-minutes-high");
    const result = recordStartAction(dir, { queueStem: "ladder-v1", minutes: 481 });
    expect(result.status).toBe(400);
  });

  test("400 on a non-integer minutes", () => {
    const dir = makeRequestsDir("start-bad-minutes-float");
    const result = recordStartAction(dir, { queueStem: "ladder-v1", minutes: 60.5 });
    expect(result.status).toBe(400);
  });

  test("503 when requestsDir is undefined, before any validation", () => {
    const result = recordStartAction(undefined, { queueStem: "ladder-v1", minutes: 60 });
    expect(result.status).toBe(503);
  });
});

describe("recordCancelAction", () => {
  test("valid body: writes target_request_id and the response states the before-claim caveat", () => {
    const dir = makeRequestsDir("cancel-valid");
    const target = "req-20260823T000000Z-0123456789abcdef";
    const result = recordCancelAction(dir, { targetRequestId: target });
    expect(result.status).toBe(200);
    if (!isAccepted(result.body)) throw new Error("expected an accepted response");
    expect(result.body.message).toContain("before dispatch claim");
    const file = readRequestFile(dir, result.body.requestId);
    expect(file).toMatchObject({ action: "cancel", target_request_id: target });
  });

  test("400 on a malformed targetRequestId", () => {
    const dir = makeRequestsDir("cancel-bad-target");
    const result = recordCancelAction(dir, { targetRequestId: "not-a-request-id" });
    expect(result.status).toBe(400);
  });

  test("400 when targetRequestId is missing entirely", () => {
    const dir = makeRequestsDir("cancel-missing-target");
    const result = recordCancelAction(dir, {});
    expect(result.status).toBe(400);
  });

  test("503 when requestsDir is undefined", () => {
    const result = recordCancelAction(undefined, { targetRequestId: "req-20260823T000000Z-0123456789abcdef" });
    expect(result.status).toBe(503);
  });
});

describe("writeControlRequestExclusive: id-collision retry (cycle-1 M3)", () => {
  test("a pre-created destination for the first-generated id forces regeneration, never overwritten", () => {
    const dir = makeRequestsDir("collision");
    const collidingId = "req-20260823T000000Z-aaaaaaaaaaaaaaaa";
    const freshId = "req-20260823T000001Z-bbbbbbbbbbbbbbbb";
    const sentinelBody = { schema: "control-request-v1", request_id: collidingId, action: "arm", requested_at: "sentinel" };
    writeFileSync(join(dir, `${collidingId}.json`), JSON.stringify(sentinelBody));

    let calls = 0;
    const idFactory = () => {
      calls += 1;
      return calls === 1 ? collidingId : freshId;
    };

    const returnedId = writeControlRequestExclusive(
      dir,
      (id, requestedAt) => ({ schema: "control-request-v1", request_id: id, action: "arm", requested_at: requestedAt }),
      idFactory,
    );

    expect(calls).toBe(2); // one collision, one successful regeneration
    expect(returnedId).toBe(freshId);
    // The pre-created (colliding) file is untouched — never overwritten, never renamed-over.
    expect(readFileSync(join(dir, `${collidingId}.json`), "utf8")).toBe(JSON.stringify(sentinelBody));
    const freshFile = readRequestFile(dir, freshId);
    expect(freshFile.request_id).toBe(freshId);
  });

  test("exhausting MAX_ID_ATTEMPTS (every candidate id collides) throws rather than overwriting", () => {
    const dir = makeRequestsDir("collision-exhausted");
    const alwaysCollidingId = "req-20260823T000000Z-cccccccccccccccc";
    writeFileSync(join(dir, `${alwaysCollidingId}.json`), JSON.stringify({ sentinel: true }));

    expect(() =>
      writeControlRequestExclusive(
        dir,
        (id, requestedAt) => ({ schema: "control-request-v1", request_id: id, action: "disarm", requested_at: requestedAt }),
        () => alwaysCollidingId,
      ),
    ).toThrow();
    expect(readFileSync(join(dir, `${alwaysCollidingId}.json`), "utf8")).toBe(JSON.stringify({ sentinel: true }));
  });
});
