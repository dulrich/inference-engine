import { describe, expect, test } from "bun:test";
import { parseControlActionResponse } from "./control.ts";

describe("parseControlActionResponse", () => {
  test("200 with a valid accepted body parses through", () => {
    const outcome = parseControlActionResponse(200, { requestId: "req-1", accepted: true, message: "recorded" });
    expect(outcome).toEqual({ ok: true, accepted: { requestId: "req-1", accepted: true, message: "recorded" } });
  });

  test("200 with a message field carries it through verbatim", () => {
    const outcome = parseControlActionResponse(200, {
      requestId: "req-2",
      accepted: true,
      message: "Stop delivered to the live run; effect is pause-after-current-cell, or a no-op if the run completes first.",
    });
    expect(outcome.ok && outcome.accepted.message).toBe(
      "Stop delivered to the live run; effect is pause-after-current-cell, or a no-op if the run completes first.",
    );
  });

  test("200 with a malformed body is a parse failure, not a crash", () => {
    const outcome = parseControlActionResponse(200, { nope: true });
    expect(outcome).toEqual({ ok: false, error: "Malformed response from the control API." });
  });

  test("400 extracts the {error} message", () => {
    const outcome = parseControlActionResponse(400, { error: "minutes must be an integer between 15 and 480" });
    expect(outcome).toEqual({ ok: false, error: "minutes must be an integer between 15 and 480" });
  });

  test("503 extracts the {error} message (control plane unconfigured)", () => {
    const outcome = parseControlActionResponse(503, {
      error: "control plane not configured: CONTROL_REQUESTS_DIR is unset",
    });
    expect(outcome).toEqual({ ok: false, error: "control plane not configured: CONTROL_REQUESTS_DIR is unset" });
  });

  test("a failure status with no parseable {error} body falls back to a status message", () => {
    const outcome = parseControlActionResponse(500, null);
    expect(outcome).toEqual({ ok: false, error: "Control action failed: HTTP 500" });
  });
});
