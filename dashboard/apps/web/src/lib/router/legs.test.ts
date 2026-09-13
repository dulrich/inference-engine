import { describe, expect, test } from "bun:test";
import type { RouterLegSummary, RouterRequestRecord } from "@bench-dashboard/shared";
import {
  ALL_LEGS,
  applyLegFilter,
  isErrorRecord,
  legChips,
  legFilterOptions,
  legKey,
  newestFirst,
  resolveLegFilter,
} from "./legs.ts";

function record(overrides: Partial<RouterRequestRecord> & { reqId: string }): RouterRequestRecord {
  return {
    kind: "request",
    ts: "2026-09-13T12:00:00.000Z",
    route: "/v1/messages",
    method: "POST",
    leg: "claude-local",
    modelResolved: "qwen38-27b-iq3s",
    sessionId: null,
    isSubagent: false,
    stream: true,
    status: 200,
    responseTokens: 10,
    reasoningTokens: 2,
    ttftMs: 120,
    wallMs: 900,
    error: null,
    ...overrides,
  } as RouterRequestRecord;
}

describe("legKey / newestFirst", () => {
  test("a null leg keys to the unattributed bucket", () => {
    expect(legKey(null)).toBe("unattributed");
    expect(legKey("codex-b")).toBe("codex-b");
  });

  test("newestFirst reverses file order on a copy, leaving the input untouched", () => {
    const delivered = [record({ reqId: "a" }), record({ reqId: "b" }), record({ reqId: "c" })];
    expect(newestFirst(delivered).map((r) => r.reqId)).toEqual(["c", "b", "a"]);
    expect(delivered.map((r) => r.reqId)).toEqual(["a", "b", "c"]);
  });
});

describe("isErrorRecord", () => {
  test("a 4xx/5xx status or a non-null error envelope is an error; 2xx and in-flight are not", () => {
    expect(isErrorRecord(record({ reqId: "a", status: 500 }))).toBe(true);
    expect(isErrorRecord(record({ reqId: "b", status: 404 }))).toBe(true);
    expect(isErrorRecord(record({ reqId: "c", status: 200, error: { stage: "upstream" } }))).toBe(true);
    expect(isErrorRecord(record({ reqId: "d", status: 200 }))).toBe(false);
    expect(isErrorRecord(record({ reqId: "e", status: null }))).toBe(false);
  });
});

describe("applyLegFilter / legFilterOptions / resolveLegFilter", () => {
  test("'all' passes everything; a leg key filters, including the unattributed bucket", () => {
    const rows = [
      record({ reqId: "a", leg: "claude-local" }),
      record({ reqId: "b", leg: "codex-b" }),
      record({ reqId: "c", leg: null }),
    ];
    expect(applyLegFilter(rows, ALL_LEGS)).toHaveLength(3);
    expect(applyLegFilter(rows, "codex-b").map((r) => r.reqId)).toEqual(["b"]);
    expect(applyLegFilter(rows, "unattributed").map((r) => r.reqId)).toEqual(["c"]);
  });

  test("options are 'all' plus each reported leg once, in response order", () => {
    const legs: RouterLegSummary[] = [
      { leg: "claude-local", requests: 3, errors: 0, p50TtftMs: 120 },
      { leg: null, requests: 1, errors: 1, p50TtftMs: null },
    ];
    expect(legFilterOptions(legs)).toEqual(["all", "claude-local", "unattributed"]);
  });

  test("a filter naming a leg that rolled out of the window falls back to 'all'", () => {
    expect(resolveLegFilter("gone-leg", ["all", "claude-local"])).toBe("all");
    expect(resolveLegFilter("claude-local", ["all", "claude-local"])).toBe("claude-local");
  });
});

describe("legChips", () => {
  test("one chip per leg with the error pill only when the leg actually errored", () => {
    const chips = legChips([
      { leg: "claude-local", requests: 3, errors: 0, p50TtftMs: 1_420 },
      { leg: null, requests: 2, errors: 1, p50TtftMs: null },
    ]);
    expect(chips).toEqual([
      { key: "claude-local", label: "claude-local", requests: 3, errors: 0, p50Label: "1.4 s", pillClass: "pill-muted" },
      { key: "unattributed", label: "unattributed", requests: 2, errors: 1, p50Label: "—", pillClass: "pill-error" },
    ]);
  });
});
