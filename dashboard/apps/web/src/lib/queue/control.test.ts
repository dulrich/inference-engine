import { describe, expect, test } from "bun:test";
import {
  armedLabel,
  armedPillClass,
  controlRequestStateLabel,
  controlRequestStatePillClass,
  lastRunDispositionLabel,
  lastRunDispositionPillClass,
  parseControlActionResponse,
  START_MINUTES_MAX,
  START_MINUTES_MIN,
  validateStartForm,
} from "./control.ts";

describe("armedLabel / armedPillClass", () => {
  test("armed", () => {
    expect(armedLabel(true)).toBe("armed");
    expect(armedPillClass(true)).toBe("pill-ok");
  });

  test("disarmed", () => {
    expect(armedLabel(false)).toBe("disarmed");
    expect(armedPillClass(false)).toBe("pill-muted");
  });
});

describe("controlRequestStatePillClass / controlRequestStateLabel", () => {
  test("maps every wire state (ControlRequestState, all 7)", () => {
    expect(controlRequestStatePillClass("pending")).toBe("pill-muted");
    expect(controlRequestStatePillClass("claimed")).toBe("pill-info");
    expect(controlRequestStatePillClass("running")).toBe("pill-info");
    expect(controlRequestStatePillClass("done")).toBe("pill-ok");
    expect(controlRequestStatePillClass("rejected")).toBe("pill-error");
    expect(controlRequestStatePillClass("canceled")).toBe("pill-warn");
    expect(controlRequestStatePillClass("launch-failed")).toBe("pill-error");
  });

  test("label passes a known state through verbatim", () => {
    expect(controlRequestStateLabel("pending")).toBe("pending");
    expect(controlRequestStateLabel("launch-failed")).toBe("launch-failed");
  });

  test("null is muted / unknown", () => {
    expect(controlRequestStatePillClass(null)).toBe("pill-muted");
    expect(controlRequestStateLabel(null)).toBe("unknown");
  });

  test("an unrecognized future wire state falls back to muted, label passes through", () => {
    expect(controlRequestStatePillClass("some-future-state")).toBe("pill-muted");
    expect(controlRequestStateLabel("some-future-state")).toBe("some-future-state");
  });
});

describe("lastRunDispositionPillClass / lastRunDispositionLabel", () => {
  test("maps every disposition (ControlLastRunDisposition, all 3)", () => {
    expect(lastRunDispositionPillClass("clean")).toBe("pill-ok");
    expect(lastRunDispositionPillClass("needs-attention")).toBe("pill-warn");
    expect(lastRunDispositionPillClass("crashed")).toBe("pill-error");
  });

  test("label passes a known disposition through verbatim", () => {
    expect(lastRunDispositionLabel("needs-attention")).toBe("needs-attention");
  });

  test("null is muted / unknown", () => {
    expect(lastRunDispositionPillClass(null)).toBe("pill-muted");
    expect(lastRunDispositionLabel(null)).toBe("unknown");
  });

  test("an unrecognized future disposition falls back to muted, label passes through", () => {
    expect(lastRunDispositionPillClass("mystery")).toBe("pill-muted");
    expect(lastRunDispositionLabel("mystery")).toBe("mystery");
  });
});

describe("validateStartForm", () => {
  test("accepts a valid stem + in-bounds minutes", () => {
    expect(validateStartForm({ queueStem: "gemma-4-12b", minutes: 60 })).toEqual({ valid: true, error: null });
  });

  test("accepts the inclusive bounds", () => {
    expect(validateStartForm({ queueStem: "x", minutes: START_MINUTES_MIN }).valid).toBe(true);
    expect(validateStartForm({ queueStem: "x", minutes: START_MINUTES_MAX }).valid).toBe(true);
  });

  test("rejects an empty (or whitespace-only) stem", () => {
    expect(validateStartForm({ queueStem: "", minutes: 60 }).valid).toBe(false);
    expect(validateStartForm({ queueStem: "   ", minutes: 60 }).valid).toBe(false);
  });

  test("rejects minutes below the floor", () => {
    const result = validateStartForm({ queueStem: "x", minutes: START_MINUTES_MIN - 1 });
    expect(result.valid).toBe(false);
    expect(result.error).not.toBeNull();
  });

  test("rejects minutes above the ceiling", () => {
    expect(validateStartForm({ queueStem: "x", minutes: START_MINUTES_MAX + 1 }).valid).toBe(false);
  });

  test("rejects a non-integer minutes value", () => {
    expect(validateStartForm({ queueStem: "x", minutes: 60.5 }).valid).toBe(false);
  });
});

describe("parseControlActionResponse", () => {
  test("200 with a valid accepted body parses through", () => {
    const outcome = parseControlActionResponse(200, { requestId: "req-1", accepted: true });
    expect(outcome.ok).toBe(true);
    expect(outcome.accepted).toEqual({ requestId: "req-1", accepted: true });
    expect(outcome.error).toBeNull();
  });

  test("200 with a message field carries it through verbatim", () => {
    const outcome = parseControlActionResponse(200, {
      requestId: "req-2",
      accepted: true,
      message: "Stop delivered to the live run; effect is pause-after-current-cell, or a no-op if the run completes first.",
    });
    expect(outcome.accepted?.message).toBe(
      "Stop delivered to the live run; effect is pause-after-current-cell, or a no-op if the run completes first.",
    );
  });

  test("200 with a malformed body is a parse failure, not a crash", () => {
    const outcome = parseControlActionResponse(200, { nope: true });
    expect(outcome.ok).toBe(false);
    expect(outcome.accepted).toBeNull();
    expect(outcome.error).not.toBeNull();
  });

  test("400 extracts the {error} message", () => {
    const outcome = parseControlActionResponse(400, { error: "minutes must be an integer between 15 and 480" });
    expect(outcome.ok).toBe(false);
    expect(outcome.error).toBe("minutes must be an integer between 15 and 480");
  });

  test("503 extracts the {error} message (control plane unconfigured)", () => {
    const outcome = parseControlActionResponse(503, {
      error: "control plane not configured: CONTROL_REQUESTS_DIR is unset",
    });
    expect(outcome.error).toBe("control plane not configured: CONTROL_REQUESTS_DIR is unset");
  });

  test("a failure status with no parseable {error} body falls back to a status message", () => {
    const outcome = parseControlActionResponse(500, null);
    expect(outcome.ok).toBe(false);
    expect(outcome.error).toBe("Control action failed: HTTP 500");
  });
});
