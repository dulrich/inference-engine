import { describe, expect, test } from "bun:test";
import { cellStateLabel, cellStatePillClass, queueStateLabel, queueStatePillClass } from "./status.ts";

describe("queueStatePillClass / queueStateLabel", () => {
  test("maps every known queue state", () => {
    expect(queueStatePillClass("running")).toBe("pill-info");
    expect(queueStatePillClass("paused-resumable")).toBe("pill-warn");
    expect(queueStatePillClass("completed")).toBe("pill-ok");
    expect(queueStatePillClass("aborted-hang")).toBe("pill-error");
  });

  test("null is muted / unknown -- no state file yet", () => {
    expect(queueStatePillClass(null)).toBe("pill-muted");
    expect(queueStateLabel(null)).toBe("unknown");
  });

  test("label passes a known state through verbatim", () => {
    expect(queueStateLabel("completed")).toBe("completed");
  });
});

describe("cellStatePillClass / cellStateLabel", () => {
  test("maps every known cell state", () => {
    expect(cellStatePillClass("pending")).toBe("pill-muted");
    expect(cellStatePillClass("running")).toBe("pill-info");
    expect(cellStatePillClass("completed")).toBe("pill-ok");
    expect(cellStatePillClass("failed")).toBe("pill-error");
    expect(cellStatePillClass("skipped-propagated")).toBe("pill-warn");
    expect(cellStatePillClass("aborted-hang")).toBe("pill-error");
  });

  test("null is muted / unjoined -- distinct wording from the queue-level unknown", () => {
    expect(cellStatePillClass(null)).toBe("pill-muted");
    expect(cellStateLabel(null)).toBe("unjoined");
  });
});
