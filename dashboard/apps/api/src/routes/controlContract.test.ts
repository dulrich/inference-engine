import { describe, expect, test } from "bun:test";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import {
  CONTROL_REQUEST_SCHEMA_V1,
  CellStateSchema,
  ControlRequestFileSchema,
  QueueListResponseSchema,
  QueueStateSchema,
} from "@bench-dashboard/shared";
import { buildControlSection, buildQueueListResponse } from "./queue.ts";

// Cross-language control-plane contract (WP-R4,
// `plans/repository-tooling-structural-remediation.md`; review finding F4). This file and
// `llama/bakeoff/tests/test_control_contract.py` read THE SAME committed bytes under
// `apps/api/fixtures/control/contract-v1/`. Python writes `status.json` and validates
// `requests/*.json`; TypeScript reads `status.json` and produces `requests/*.json` — so the
// fixtures are the one place the two implementations meet, and a drift on either side fails on
// the other side's suite too.
//
// The fixture tree is a real `<state-dir>/control` subtree, not a flattened convenience shape:
// `control/host/status.json` is the byte-for-byte output of one Python `dispatch_pass` over
// `control/requests/` + `control/host/live.json` + `control/host/results/`.

const CONTRACT_DIR = join(import.meta.dir, "../../fixtures/control/contract-v1");
const REQUESTS_DIR = join(CONTRACT_DIR, "control/requests");
const HOST_DIR = join(CONTRACT_DIR, "control/host");
const QUEUES_DIR = join(CONTRACT_DIR, "queues");

/** The disposition reason the Python dispatcher writes for WP-R4's schema rejection
 * (`control_protocol.REQUEST_SCHEMA_REJECTION`). Pinned here because it arrives on this side as
 * opaque wire text rendered straight into the UI. */
const SCHEMA_REJECTION_REASON = "malformed: schema is not control-request-v1";

const REQ_DISARM = "req-20260827T120000Z-00000000000000a1";
const REQ_ARM = "req-20260827T120100Z-00000000000000a2";
const REQ_START = "req-20260827T120200Z-00000000000000a3";
const REQ_STOP = "req-20260827T120300Z-00000000000000a4";
const REQ_CANCEL = "req-20260827T120400Z-00000000000000a5";
const REQ_SCHEMA_ABSENT = "req-20260827T120500Z-00000000000000b1";
const REQ_SCHEMA_UNKNOWN = "req-20260827T120600Z-00000000000000b2";
const REQ_SCHEMA_WRONG_TYPE = "req-20260827T120700Z-00000000000000b3";

const VALID_REQUESTS: ReadonlyArray<readonly [string, string]> = [
  [REQ_DISARM, "disarm"],
  [REQ_ARM, "arm"],
  [REQ_START, "start"],
  [REQ_STOP, "stop"],
  [REQ_CANCEL, "cancel"],
];
const SCHEMA_REJECTS: ReadonlyArray<readonly [string, string]> = [
  [REQ_SCHEMA_ABSENT, "absent"],
  [REQ_SCHEMA_UNKNOWN, "unknown"],
  [REQ_SCHEMA_WRONG_TYPE, "wrong-typed"],
];

function readRequestFixture(requestId: string): unknown {
  return JSON.parse(readFileSync(join(REQUESTS_DIR, `${requestId}.json`), "utf8")) as unknown;
}

describe("control-request-v1 fixtures: the same bytes the Python validator reads", () => {
  test("the fixture set is exactly the eight committed request files", () => {
    expect(readdirSync(REQUESTS_DIR).sort()).toEqual(
      [...VALID_REQUESTS, ...SCHEMA_REJECTS].map(([id]) => `${id}.json`).sort(),
    );
  });

  for (const [requestId, action] of VALID_REQUESTS) {
    test(`${action}: parses against ControlRequestFileSchema`, () => {
      const parsed = ControlRequestFileSchema.safeParse(readRequestFixture(requestId));
      expect(parsed.success).toBe(true);
      if (!parsed.success) return;
      expect(parsed.data.schema).toBe(CONTROL_REQUEST_SCHEMA_V1);
      expect(parsed.data.action).toBe(action as never);
      expect(parsed.data.request_id).toBe(requestId);
    });
  }

  test("start carries queue_stem + minutes; cancel carries target_request_id", () => {
    const start = ControlRequestFileSchema.parse(readRequestFixture(REQ_START));
    expect(start.queue_stem).toBe("contract-queue");
    expect(start.minutes).toBe(60);
    const cancel = ControlRequestFileSchema.parse(readRequestFixture(REQ_CANCEL));
    expect(cancel.target_request_id).toBe(REQ_START);
  });

  for (const [requestId, flavour] of SCHEMA_REJECTS) {
    test(`${flavour} schema: rejected on the schema field, same verdict as the Python validator`, () => {
      const parsed = ControlRequestFileSchema.safeParse(readRequestFixture(requestId));
      expect(parsed.success).toBe(false);
      if (parsed.success) return;
      expect(parsed.error.issues.some((issue) => issue.path[0] === "schema")).toBe(true);
    });
  }
});

describe("status.json: the file Python writes and this API reads", () => {
  test("buildControlSection projects the committed status.json into a valid ControlSection", async () => {
    const section = await buildControlSection(HOST_DIR);
    expect(section).not.toBeNull();
    if (section === null) return;

    expect(section.statusGeneratedAt).toBe("2026-08-27T12:10:00Z");
    // `stale` is computed at read time from `generated_at` vs now, never read from the file —
    // derived here the same way rather than pinned, so this never becomes a clock-dependent flake.
    expect(section.stale).toBe(Date.now() - Date.parse("2026-08-27T12:10:00Z") > 180_000);
    expect(section.armed).toBe(true);
    expect(section.live).toEqual({
      requestId: "nightly-20260827",
      queueStem: "contract-queue",
      phase: "running",
      startedAt: "2026-08-27T11:00:00Z",
    });
    expect(section.lastRun).toEqual({
      requestId: "inv-contract-0002",
      endedAt: "2026-08-27T09:00:00Z",
      runExit: 0,
      verifyExit: 0,
      restartFailed: null,
      disposition: "clean",
    });
    expect(section.anomalies).toEqual([]);
  });

  test("every request digest row round-trips, including the three schema rejections", async () => {
    const section = await buildControlSection(HOST_DIR);
    if (section === null) throw new Error("expected a control section");
    expect(section.requests.map((r) => [r.requestId, r.action, r.state, r.reason])).toEqual([
      [REQ_DISARM, "disarm", "done", null],
      [REQ_ARM, "arm", "done", null],
      [REQ_START, "start", "claimed", null],
      [REQ_STOP, "stop", "done", "stop-delivered"],
      [REQ_CANCEL, "cancel", "rejected", "already-claimed"],
      [REQ_SCHEMA_ABSENT, "arm", "rejected", SCHEMA_REJECTION_REASON],
      [REQ_SCHEMA_UNKNOWN, "start", "rejected", SCHEMA_REJECTION_REASON],
      [REQ_SCHEMA_WRONG_TYPE, "stop", "rejected", SCHEMA_REJECTION_REASON],
    ]);
  });

  test("a rejected request still renders — the dispatcher's digest never hides one", async () => {
    const section = await buildControlSection(HOST_DIR);
    if (section === null) throw new Error("expected a control section");
    expect(section.requests.filter((r) => r.reason === SCHEMA_REJECTION_REASON)).toHaveLength(3);
  });
});

describe("queue + queue-state fixtures: the pair batch.py writes and this API joins", () => {
  test("buildQueueListResponse joins the committed pair by alias", async () => {
    const response = await buildQueueListResponse(QUEUES_DIR);
    expect(QueueListResponseSchema.safeParse(response).success).toBe(true);
    expect(response.queues).toHaveLength(1);
    const entry = response.queues[0];
    if (entry === undefined || !("cells" in entry)) throw new Error("expected a successfully parsed queue entry");

    expect(entry.name).toBe("contract-queue");
    expect(entry.queueState).toBe("paused-resumable");
    expect(entry.deadline).toBe("07:00");
    expect(entry.vramProfile).toBe("desktop-shared");
    expect(entry.cellCounts).toEqual({
      pending: 1,
      running: 0,
      completed: 1,
      failed: 1,
      "skipped-propagated": 0,
      "aborted-hang": 0,
    });
    expect(entry.cells.map((c) => [c.alias, c.state, c.reason])).toEqual([
      ["contract-a", "completed", null],
      ["contract-b", "failed", "contract fixture failure"],
      ["contract-c", "pending", null],
    ]);
  });
});

describe("vocabularies are derived from the shared zod enums, never re-typed", () => {
  // Membership pin (WP-R4 is a de-duplication, not a redefinition): these are the exact tuples
  // `routes/queue.ts` used to carry as parallel literal arrays. `queue.ts` now reads
  // `CellStateSchema.options` / `QueueStateSchema.options` instead, so this is the one place the
  // vocabulary is written down twice — deliberately, as the lock.
  test("CellStateSchema.options is unchanged", () => {
    expect(CellStateSchema.options).toEqual([
      "pending",
      "running",
      "completed",
      "failed",
      "skipped-propagated",
      "aborted-hang",
    ]);
  });

  test("QueueStateSchema.options is unchanged", () => {
    expect(QueueStateSchema.options).toEqual(["running", "paused-resumable", "completed", "aborted-hang"]);
  });

  test("cellCounts keys follow the enum, in enum order", async () => {
    const response = await buildQueueListResponse(QUEUES_DIR);
    const entry = response.queues[0];
    if (entry === undefined || !("cellCounts" in entry)) throw new Error("expected a successfully parsed queue entry");
    expect(Object.keys(entry.cellCounts)).toEqual([...CellStateSchema.options]);
  });
});
