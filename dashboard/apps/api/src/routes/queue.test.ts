import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { QueueEntry, QueueErrorEntry, QueueListItem } from "@bench-dashboard/shared";
import { buildQueueListResponse } from "./queue.ts";

// Tests `buildQueueListResponse` directly over an explicit `queuesDir` — NOT through
// `registerQueueRoutes`/`config.queuesDir`/`QUEUES_DIR`. `config.ts` is a singleton
// computed once at module load and shared across every test FILE in one `bun test`
// process (not per-file), so a per-scenario env-var override here would only reliably
// win for whichever scenario's file happens to import `config.ts` first anywhere in the
// whole run — the queue route's OWN logic is exercised fully via this seam instead;
// `app.routes.test.ts` covers the one thing this can't: that `registerQueueRoutes` is
// actually wired into `app.ts` before the SPA wildcard.

const FIXTURES_DIR = join(import.meta.dir, "../../fixtures/queues");

function isEntry(item: QueueListItem): item is QueueEntry {
  return !("error" in item);
}
function isError(item: QueueListItem): item is QueueErrorEntry {
  return "error" in item;
}

describe("buildQueueListResponse: missing/empty queues dir", () => {
  test("a queues dir that does not exist on disk at all -> {queues: []}, never throws", async () => {
    const missing = join(mkdtempSync(join(tmpdir(), "bench-dashboard-queue-missing-")), "does-not-exist");
    const response = await buildQueueListResponse(missing);
    expect(response.queues).toEqual([]);
    expect(typeof response.generatedAt).toBe("string");
  });

  test("a queues dir that exists but is empty -> {queues: []}", async () => {
    const empty = mkdtempSync(join(tmpdir(), "bench-dashboard-queue-empty-"));
    const response = await buildQueueListResponse(empty);
    expect(response.queues).toEqual([]);
  });
});

describe("buildQueueListResponse: fixtures/queues", () => {
  test("returns one entry per non-.state.json queue file", async () => {
    const response = await buildQueueListResponse(FIXTURES_DIR);
    const names = response.queues.map((q) => q.name).sort();
    expect(names).toEqual(
      [
        "malformed-shape-cells-not-array",
        "malformed-state-pair",
        "malformed-truncated",
        "no-state-queue",
        "synth-joined",
        "wp5-validation",
      ].sort(),
    );
  });

  describe("joined case (synth-joined: new-format state file, alias present)", () => {
    test("queueState/deadline/vramProfile/cellCounts reflect the state + queue files", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "synth-joined");
      if (!entry || !isEntry(entry)) throw new Error("expected a joined queue entry");
      expect(entry.queueState).toBe("completed");
      expect(entry.deadline).toBe("13:00");
      expect(entry.vramProfile).toBe("desktop-shared");
      expect(entry.cellCounts).toEqual({
        pending: 1,
        running: 1,
        completed: 1,
        failed: 1,
        "skipped-propagated": 0,
        "aborted-hang": 0,
      });
    });

    test("joins a cell by top-level state-cell alias", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "synth-joined");
      if (!entry || !isEntry(entry)) throw new Error("expected a joined queue entry");
      const cell = entry.cells.find((c) => c.alias === "gemma-qat-c32768-f16-prod");
      expect(cell).toMatchObject({ state: "completed", reason: null, model: "gemma-4-12b-qat", c: 32768, ctk: "f16" });
    });

    test("joins a cell by detail.alias fallback when the top-level alias key is absent", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "synth-joined");
      if (!entry || !isEntry(entry)) throw new Error("expected a joined queue entry");
      const cell = entry.cells.find((c) => c.alias === "gemma-qat-detail-fallback");
      expect(cell).toMatchObject({ state: "running", reason: null });
    });

    test("a queue cell whose alias matches no state entry renders unjoined (state/reason null)", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "synth-joined");
      if (!entry || !isEntry(entry)) throw new Error("expected a joined queue entry");
      const cell = entry.cells.find((c) => c.alias === "gemma-qat-unmatched");
      expect(cell).toMatchObject({ state: null, reason: null });
    });

    test("queue-side fields (samplingArm/trials/ctk/ctv/fa) pass through per cell alongside the join", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "synth-joined");
      if (!entry || !isEntry(entry)) throw new Error("expected a joined queue entry");
      const cell = entry.cells.find((c) => c.alias === "gemma-qat-c32768-q8q8-cap");
      expect(cell).toMatchObject({
        samplingArm: "capability",
        trials: 3,
        ctk: "q8_0",
        ctv: "q8_0",
        fa: "on",
        state: "failed",
        reason: "needle_failure",
      });
    });
  });

  describe("unjoined case (wp5-validation: real committed old-format pair, no alias anywhere)", () => {
    test("queueState/deadline/vramProfile still come from the queue+state files", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "wp5-validation");
      if (!entry || !isEntry(entry)) throw new Error("expected a queue entry");
      expect(entry.queueState).toBe("completed");
      expect(entry.deadline).toBe("13:00");
      expect(entry.vramProfile).toBe("desktop-shared");
    });

    test("every cell renders unjoined: state and reason are null despite a state file existing", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "wp5-validation");
      if (!entry || !isEntry(entry)) throw new Error("expected a queue entry");
      expect(entry.cells.length).toBeGreaterThan(0);
      for (const cell of entry.cells) {
        expect(cell.state).toBeNull();
        expect(cell.reason).toBeNull();
      }
      const first = entry.cells.find((c) => c.alias === "gemma-qat-c32768-f16-prod");
      expect(first).toMatchObject({ model: "gemma-4-12b-qat", c: 32768, samplingArm: "production", trials: 3 });
    });

    test("cellCounts is still computed from the (unjoined) state file's own cell states", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "wp5-validation");
      if (!entry || !isEntry(entry)) throw new Error("expected a queue entry");
      // 5 cells in the real wp5-validation state file: 2 completed, 3 failed.
      expect(entry.cellCounts.completed).toBe(2);
      expect(entry.cellCounts.failed).toBe(3);
      expect(entry.cellCounts.pending).toBe(0);
    });
  });

  describe("no sibling state file at all (no-state-queue)", () => {
    test("queueState is null and cellCounts is all-null, never zero", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "no-state-queue");
      if (!entry || !isEntry(entry)) throw new Error("expected a queue entry");
      expect(entry.queueState).toBeNull();
      expect(entry.cellCounts).toEqual({
        pending: null,
        running: null,
        completed: null,
        failed: null,
        "skipped-propagated": null,
        "aborted-hang": null,
      });
      expect(entry.cells[0]).toMatchObject({ state: null, reason: null });
    });
  });

  describe("malformed files never abort the whole response", () => {
    test("invalid JSON in the queue file itself reports {name, error}", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "malformed-truncated");
      if (!entry) throw new Error("expected an entry for malformed-truncated");
      expect(isError(entry)).toBe(true);
      if (isError(entry)) expect(typeof entry.error).toBe("string");
    });

    test("a queue file with the wrong shape (cells not an array) reports {name, error}", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "malformed-shape-cells-not-array");
      if (!entry) throw new Error("expected an entry for malformed-shape-cells-not-array");
      expect(isError(entry)).toBe(true);
    });

    test("a valid queue file with an invalid sibling state file reports {name, error} for that queue", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "malformed-state-pair");
      if (!entry) throw new Error("expected an entry for malformed-state-pair");
      expect(isError(entry)).toBe(true);
    });

    test("one malformed file never prevents other queues from parsing correctly", async () => {
      const { queues } = await buildQueueListResponse(FIXTURES_DIR);
      const entry = queues.find((q) => q.name === "synth-joined");
      if (!entry || !isEntry(entry)) throw new Error("synth-joined should still parse fine alongside malformed siblings");
      expect(entry.queueState).toBe("completed");
    });
  });
});
