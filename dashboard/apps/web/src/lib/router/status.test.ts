import { describe, expect, test } from "bun:test";
import { unavailableMessage } from "./status.ts";

describe("unavailableMessage", () => {
  test("each unavailable reason renders in words", () => {
    expect(unavailableMessage("disabled")).toBe("not configured");
    expect(unavailableMessage("unreadable")).toBe("log directory unreadable");
    expect(unavailableMessage("empty")).toBe("no records yet");
  });
});
