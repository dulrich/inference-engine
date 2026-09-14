import { describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  SERVING_STATUS_STALE_MS,
  ServingRequestSchema,
  ServingStatusFileSchema,
  type ServingAcceptedResponse,
} from "@bench-dashboard/shared";
import {
  SERVING_STATUS_BASENAME,
  generateServingRequestId,
  readServingStatus,
  recordServingVisibilityAction,
} from "./servingControl.ts";

// Tests `recordServingVisibilityAction`/`readServingStatus` directly over an explicit
// directory (and, for the status read, an injected `now`) — the same seam pattern as
// `control.test.ts`, for the same reason: `config.ts` is a singleton shared across the whole
// `bun test` process, and a 180 s staleness boundary is not testable against a wall clock.
//
// The status fixture is the COMMITTED cross-language contract file
// (`apps/api/fixtures/serving/contract-v1/control/host/serving-status.json`), which carries
// the real 17-key visibility matrix from `router/visibility.json`; WP-5's Python validator
// reads the same bytes.

const CONTRACT_DIR = join(import.meta.dir, "../../fixtures/serving/contract-v1");
const CONTRACT_HOST_DIR = join(CONTRACT_DIR, "control/host");
const CONTRACT_REQUESTS_DIR = join(CONTRACT_DIR, "control/requests");
const CONTRACT_INVALID_DIR = join(CONTRACT_DIR, "invalid");

/** The fixture's own `generated_at`, so staleness is measured against the committed bytes
 * rather than against a synthesized file. */
const FIXTURE_GENERATED_AT_MS = Date.parse("2026-09-13T12:00:00.000Z");

const SERVING_REQUEST_ID_GRAMMAR = /^srv-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$/;

function makeRequestsDir(prefix: string): string {
  return mkdtempSync(join(tmpdir(), `bench-dashboard-serving-requests-${prefix}-`));
}

function makeHostDir(prefix: string): string {
  return mkdtempSync(join(tmpdir(), `bench-dashboard-serving-host-${prefix}-`));
}

function isAccepted(body: unknown): body is ServingAcceptedResponse {
  return typeof body === "object" && body !== null && (body as { accepted?: unknown }).accepted === true;
}

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, "utf8")) as unknown;
}

describe("generateServingRequestId", () => {
  test("matches the pinned grammar srv-<UTCcompact>-<16 hex>", () => {
    expect(generateServingRequestId()).toMatch(SERVING_REQUEST_ID_GRAMMAR);
  });

  test("is distinct from the bake-off plane's `req-` grammar (the two share one drop-box)", () => {
    expect(generateServingRequestId().startsWith("srv-")).toBe(true);
  });
});

describe("recordServingVisibilityAction", () => {
  test("writes exactly ONE file whose bytes parse under ServingRequestSchema", () => {
    const dir = makeRequestsDir("write");
    const result = recordServingVisibilityAction(dir, {
      modelKey: "Qwen3.8-27B-UD-IQ3_S",
      consumers: ["cc", "openwebui"],
    });
    expect(result.status).toBe(200);
    if (!isAccepted(result.body)) throw new Error("expected an accepted response");
    expect(result.body.requestId).toMatch(SERVING_REQUEST_ID_GRAMMAR);
    expect(result.body.message).toContain("next dispatcher pass");

    const written = readdirSync(dir);
    expect(written).toEqual([`${result.body.requestId}.json`]);

    const parsed = ServingRequestSchema.parse(readJson(join(dir, written[0] as string)));
    expect(parsed).toMatchObject({
      schema: "serving-request-v1",
      request_id: result.body.requestId,
      action: "set-visibility",
      model_key: "Qwen3.8-27B-UD-IQ3_S",
      consumers: ["cc", "openwebui"],
    });
    // Millisecond-resolution ISO-8601 UTC: the dispatcher orders same-model requests by
    // `(requested_at, request_id)`, and the id's whole-second stamp does not order.
    expect(parsed.requested_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  });

  test("an empty `consumers` array is valid — that is the unpublish request", () => {
    const dir = makeRequestsDir("empty-consumers");
    const result = recordServingVisibilityAction(dir, {
      modelKey: "Qwen3.8-27B-UD-IQ3_S-reasoning-off",
      consumers: [],
    });
    expect(result.status).toBe(200);
    if (!isAccepted(result.body)) throw new Error("expected an accepted response");
    const parsed = ServingRequestSchema.parse(readJson(join(dir, `${result.body.requestId}.json`)));
    expect(parsed.consumers).toEqual([]);
  });

  test("`cc` on any model key is accepted — the route has no declaration to check it against", () => {
    const dir = makeRequestsDir("cc-unchecked");
    // `bge-m3-f16` has `gateway_id: null`; the dispatcher rejects this at ingest and the UI
    // disables the chip, but `router/visibility.json` is not mounted here, so the ROUTE
    // cannot and does not pre-validate it.
    const result = recordServingVisibilityAction(dir, { modelKey: "bge-m3-f16", consumers: ["cc"] });
    expect(result.status).toBe(200);
  });

  test("collision-safe exclusive create: a repeated id is retried, never overwritten", () => {
    const dir = makeRequestsDir("collision");
    const ids = ["srv-20260913T120000Z-00000000000000aa", "srv-20260913T120000Z-00000000000000bb"];
    let call = 0;
    // First two calls hand back the SAME id; the third is distinct. The second request must
    // therefore retry rather than clobber the first.
    const idFactory = () => {
      const id = call === 0 || call === 1 ? (ids[0] as string) : (ids[1] as string);
      call += 1;
      return id;
    };
    const first = recordServingVisibilityAction(dir, { modelKey: "a", consumers: [] }, idFactory);
    const second = recordServingVisibilityAction(dir, { modelKey: "b", consumers: ["openwebui"] }, idFactory);
    if (!isAccepted(first.body) || !isAccepted(second.body)) throw new Error("expected accepted responses");
    expect(first.body.requestId).toBe(ids[0] as string);
    expect(second.body.requestId).toBe(ids[1] as string);
    expect(readdirSync(dir).sort()).toEqual([`${ids[0]}.json`, `${ids[1]}.json`]);
    // The first request's body survived intact — no destructive rename over it.
    expect(ServingRequestSchema.parse(readJson(join(dir, `${ids[0]}.json`))).model_key).toBe("a");
  });

  test("400 on an unknown consumer token, and nothing is written", () => {
    const dir = makeRequestsDir("bad-consumer");
    const result = recordServingVisibilityAction(dir, { modelKey: "Qwen3.8-27B-UD-IQ3_S", consumers: ["ollama"] });
    expect(result.status).toBe(400);
    expect(readdirSync(dir)).toEqual([]);
  });

  test("400 on an empty modelKey, a missing modelKey, and a non-array consumers", () => {
    const dir = makeRequestsDir("bad-shape");
    expect(recordServingVisibilityAction(dir, { modelKey: "", consumers: [] }).status).toBe(400);
    expect(recordServingVisibilityAction(dir, { consumers: ["cc"] }).status).toBe(400);
    expect(recordServingVisibilityAction(dir, { modelKey: "a", consumers: "cc" }).status).toBe(400);
    expect(recordServingVisibilityAction(dir, {}).status).toBe(400);
    expect(readdirSync(dir)).toEqual([]);
  });

  test("503 with a body naming the missing config when requestsDir is undefined", () => {
    const result = recordServingVisibilityAction(undefined, { modelKey: "a", consumers: [] });
    expect(result.status).toBe(503);
    expect(JSON.stringify(result.body)).toContain("CONTROL_REQUESTS_DIR");
  });
});

describe("readServingStatus", () => {
  test("available: the committed fixture's whole 17-key visibility map, 179 s old", () => {
    const now = new Date(FIXTURE_GENERATED_AT_MS + 179_000);
    const status = readServingStatus(CONTRACT_HOST_DIR, now);
    if (!status.available) throw new Error(`expected available, got ${status.reason}`);
    expect(status.schema).toBe("serving-status-v1");
    expect(Object.keys(status.visibility)).toHaveLength(17);
    expect(status.visibility["Qwen3.8-27B-UD-IQ3_S"]).toEqual({
      gateway_id: "qwen38-27b-iq3s",
      consumers: ["cc", "openwebui"],
    });
    // Ten of the seventeen keys have no picker crosswalk — the `cc` chip is disabled there.
    const nullGateway = Object.values(status.visibility).filter((entry) => entry.gateway_id === null);
    expect(nullGateway).toHaveLength(10);
    expect(status.last_request?.request_id).toBe("srv-20260913T115930Z-00000000000000a2");
    expect(status.recent.map((record) => record.state)).toEqual(["applied", "applied", "rejected", "failed"]);
    expect(status.recent[2]?.reason).toBe("cc requires a gateway id");
  });

  test("stale at 181 s, available at 179 s — the 180 s boundary itself is NOT stale", () => {
    expect(readServingStatus(CONTRACT_HOST_DIR, new Date(FIXTURE_GENERATED_AT_MS + 181_000))).toEqual({
      available: false,
      reason: "stale",
    });
    expect(readServingStatus(CONTRACT_HOST_DIR, new Date(FIXTURE_GENERATED_AT_MS + 179_000)).available).toBe(true);
    expect(
      readServingStatus(CONTRACT_HOST_DIR, new Date(FIXTURE_GENERATED_AT_MS + SERVING_STATUS_STALE_MS)).available,
    ).toBe(true);
    expect(
      readServingStatus(CONTRACT_HOST_DIR, new Date(FIXTURE_GENERATED_AT_MS + SERVING_STATUS_STALE_MS + 1)),
    ).toEqual({ available: false, reason: "stale" });
  });

  test("disabled when CONTROL_HOST_DIR is unset", () => {
    expect(readServingStatus(undefined)).toEqual({ available: false, reason: "disabled" });
  });

  test("unreadable when the directory has no serving-status.json (the stale-mount shape)", () => {
    expect(readServingStatus(makeHostDir("missing"))).toEqual({ available: false, reason: "unreadable" });
  });

  test("empty when the file exists but holds no bytes", () => {
    const dir = makeHostDir("empty");
    writeFileSync(join(dir, SERVING_STATUS_BASENAME), "   \n");
    expect(readServingStatus(dir)).toEqual({ available: false, reason: "empty" });
  });

  test("unreadable on non-JSON bytes, on a schema mismatch, and on an unparseable generated_at", () => {
    const dir = makeHostDir("unreadable");
    const path = join(dir, SERVING_STATUS_BASENAME);

    writeFileSync(path, "{not json");
    expect(readServingStatus(dir)).toEqual({ available: false, reason: "unreadable" });

    // The committed wrong-schema fixture: a `serving-status-v2` file is not tolerated.
    writeFileSync(path, readFileSync(join(CONTRACT_INVALID_DIR, "status-schema-unknown.json"), "utf8"));
    expect(readServingStatus(dir)).toEqual({ available: false, reason: "unreadable" });

    writeFileSync(
      path,
      JSON.stringify({
        schema: "serving-status-v1",
        generated_at: "whenever",
        visibility: {},
        last_request: null,
        recent: [],
      }),
    );
    expect(readServingStatus(dir)).toEqual({ available: false, reason: "unreadable" });
  });

  test("a status whose visibility map is empty is still available — drift is the UI's verdict, per row", () => {
    const dir = makeHostDir("empty-map");
    writeFileSync(
      join(dir, SERVING_STATUS_BASENAME),
      JSON.stringify({
        schema: "serving-status-v1",
        generated_at: new Date().toISOString(),
        visibility: {},
        last_request: null,
        recent: [],
      }),
    );
    const status = readServingStatus(dir);
    if (!status.available) throw new Error(`expected available, got ${status.reason}`);
    expect(status.visibility).toEqual({});
  });
});

describe("serving contract fixtures (the same bytes WP-5's Python validator reads)", () => {
  test("every committed request fixture parses under ServingRequestSchema, id agreeing with basename", () => {
    const names = readdirSync(CONTRACT_REQUESTS_DIR).sort();
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) {
      const parsed = ServingRequestSchema.parse(readJson(join(CONTRACT_REQUESTS_DIR, name)));
      expect(`${parsed.request_id}.json`).toBe(name);
    }
  });

  test("the status fixture parses under ServingStatusFileSchema", () => {
    expect(() => ServingStatusFileSchema.parse(readJson(join(CONTRACT_HOST_DIR, SERVING_STATUS_BASENAME)))).not.toThrow();
  });

  test("every committed invalid request fixture is REJECTED by ServingRequestSchema", () => {
    const names = readdirSync(CONTRACT_INVALID_DIR)
      .filter((name) => name.startsWith("request-"))
      .sort();
    expect(names).toEqual([
      "request-consumer-unknown.json",
      "request-id-grammar.json",
      "request-model-key-empty.json",
      "request-schema-unknown.json",
    ]);
    for (const name of names) {
      expect(ServingRequestSchema.safeParse(readJson(join(CONTRACT_INVALID_DIR, name))).success).toBe(false);
    }
  });
});
