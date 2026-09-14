import { describe, expect, test } from "bun:test";
import type { ServingStatusResponse } from "@bench-dashboard/shared";
import {
  SERVING_CONSUMERS,
  canonicalConsumers,
  chipState,
  consumerLabel,
  isRequestSettled,
  nextConsumers,
  requestOutcome,
  rowChips,
  settlePending,
  unavailableReasonText,
  visibilityEntry,
  visibilityRequestBody,
  type PendingVisibilityMap,
} from "./visibility.ts";

// The chips' whole decision table, exercised against the same shapes the API returns. The
// map here mirrors the committed contract fixture's matrix: a both-consumers gateway row, a
// null-gateway row that IS published to Open WebUI, and a null-gateway row published nowhere.

const REQ_A = "srv-20260913T115930Z-00000000000000a2";
const REQ_B = "srv-20260913T120500Z-00000000000000d1";

function status(overrides: Partial<Extract<ServingStatusResponse, { available: true }>> = {}): ServingStatusResponse {
  return {
    available: true,
    schema: "serving-status-v1",
    generated_at: "2026-09-13T12:00:00.000Z",
    visibility: {
      "Qwen3.8-27B-UD-IQ3_S": { gateway_id: "qwen38-27b-iq3s", consumers: ["cc", "openwebui"] },
      "Ling-3.0-tiny-Q8_0": { gateway_id: null, consumers: ["openwebui"] },
      "bge-m3-f16": { gateway_id: null, consumers: [] },
      "nemotron-3-nano-4B-UD-Q4_K_XL": { gateway_id: "nemotron-4b", consumers: ["openwebui"] },
    },
    last_request: null,
    recent: [],
    ...overrides,
  };
}

describe("consumer vocabulary helpers", () => {
  test("the canonical order is [cc, openwebui] and both carry product-surface labels", () => {
    expect(SERVING_CONSUMERS).toEqual(["cc", "openwebui"]);
    expect(consumerLabel("cc")).toBe("Claude Code");
    expect(consumerLabel("openwebui")).toBe("Open WebUI");
  });

  test("canonicalConsumers orders and deduplicates", () => {
    expect(canonicalConsumers(["openwebui", "cc"])).toEqual(["cc", "openwebui"]);
    expect(canonicalConsumers(["openwebui", "openwebui"])).toEqual(["openwebui"]);
    expect(canonicalConsumers([])).toEqual([]);
  });

  test("nextConsumers builds the FULL desired array, never a delta", () => {
    expect(nextConsumers(["openwebui"], "cc", true)).toEqual(["cc", "openwebui"]);
    expect(nextConsumers(["cc", "openwebui"], "cc", false)).toEqual(["openwebui"]);
    expect(nextConsumers(["openwebui"], "openwebui", false)).toEqual([]);
    // Idempotent: turning on something already on is not a duplicate.
    expect(nextConsumers(["cc", "openwebui"], "openwebui", true)).toEqual(["cc", "openwebui"]);
  });

  test("visibilityRequestBody carries the model key and the normalized desired array", () => {
    expect(
      visibilityRequestBody("Ling-3.0-tiny-Q8_0", { gateway_id: null, consumers: ["openwebui"] }, "openwebui", false),
    ).toEqual({ modelKey: "Ling-3.0-tiny-Q8_0", consumers: [] });
  });
});

describe("unavailableReasonText", () => {
  test("all four reasons read as words, and `stale` names the host dispatcher", () => {
    expect(unavailableReasonText("disabled")).toContain("not configured");
    expect(unavailableReasonText("unreadable")).toContain("unreadable");
    expect(unavailableReasonText("empty")).toContain("no status");
    expect(unavailableReasonText("stale")).toContain("host dispatcher");
  });
});

describe("visibilityEntry", () => {
  test("returns the declared entry, null for an unavailable status and null for a drifted key", () => {
    expect(visibilityEntry(status(), "bge-m3-f16")).toEqual({ gateway_id: null, consumers: [] });
    expect(visibilityEntry(status(), "not-served-here")).toBeNull();
    expect(visibilityEntry({ available: false, reason: "stale" }, "bge-m3-f16")).toBeNull();
    expect(visibilityEntry(null, "bge-m3-f16")).toBeNull();
  });
});

describe("chipState: the status map is the only source", () => {
  test("enabled chips reflect the declared consumers array", () => {
    const cc = chipState("cc", "Qwen3.8-27B-UD-IQ3_S", status());
    expect(cc).toMatchObject({ kind: "enabled", on: true, disabled: false, stateLabel: "on", pillClass: "pill-ok" });
    const owui = chipState("openwebui", "bge-m3-f16", status());
    expect(owui).toMatchObject({ kind: "enabled", on: false, disabled: false, stateLabel: "off", pillClass: "pill-muted" });
  });

  test("a null status disables both chips with a loading/error reason, never a guessed state", () => {
    const chip = chipState("openwebui", "bge-m3-f16", null);
    expect(chip).toMatchObject({ kind: "disabled", disabled: true, on: false, stateLabel: "unknown" });
    expect(chip.reason).toContain("not loaded");
    expect(chipState("cc", "bge-m3-f16", null, {}, "HTTP 500").reason).toBe("HTTP 500");
  });

  test("every unavailable reason — including STALE — disables the chips with that reason", () => {
    for (const reason of ["disabled", "unreadable", "empty", "stale"] as const) {
      const chip = chipState("openwebui", "Qwen3.8-27B-UD-IQ3_S", { available: false, reason });
      expect(chip.kind).toBe("disabled");
      expect(chip.disabled).toBe(true);
      expect(chip.reason).toBe(unavailableReasonText(reason));
      // Crucially NOT the declared value from some other source: a stale map renders unknown.
      expect(chip.stateLabel).toBe("unknown");
    }
  });

  test("a key served by GET /api/serving but absent from the status map is disabled as drift", () => {
    const chip = chipState("openwebui", "gemma-4-12B-it-qat-UD-Q4_K_XL", status());
    expect(chip.kind).toBe("disabled");
    expect(chip.reason).toContain("drift");
    expect(chip.stateLabel).toBe("unknown");
  });

  test("the cc chip is disabled with `not a gateway model` on a null gateway_id row", () => {
    const chip = chipState("cc", "Ling-3.0-tiny-Q8_0", status());
    expect(chip).toMatchObject({ kind: "disabled", disabled: true, reason: "not a gateway model" });
    expect(chip.title).toContain("Ling-3.0-tiny-Q8_0");
    // …and the openwebui chip on that SAME row stays live.
    expect(chipState("openwebui", "Ling-3.0-tiny-Q8_0", status())).toMatchObject({ kind: "enabled", on: true });
  });

  test("the cc disable outranks a pending request on the same row", () => {
    const pending: PendingVisibilityMap = { "bge-m3-f16": { requestId: REQ_B, consumers: ["cc"] } };
    expect(chipState("cc", "bge-m3-f16", status(), pending).kind).toBe("disabled");
  });
});

describe("chipState: the optimistic requested state", () => {
  const pending: PendingVisibilityMap = {
    "Qwen3.8-27B-UD-IQ3_S": { requestId: REQ_B, consumers: ["openwebui"] },
  };

  test("a pending request renders `requested`, reflecting the REQUESTED array and the id", () => {
    const cc = chipState("cc", "Qwen3.8-27B-UD-IQ3_S", status(), pending);
    expect(cc).toMatchObject({ kind: "requested", on: false, requestId: REQ_B, stateLabel: "requested", pillClass: "pill-info" });
    // Locked until `recent[]` settles it: a second click would build its body from the
    // declared array, which is not what is in flight.
    expect(cc.disabled).toBe(true);
    expect(cc.reason).toBeNull();
    expect(cc.title).toContain("next dispatcher pass");
    expect(chipState("openwebui", "Qwen3.8-27B-UD-IQ3_S", status(), pending)).toMatchObject({
      kind: "requested",
      on: true,
    });
  });

  test("a pending mark on one row never touches another row", () => {
    expect(chipState("openwebui", "bge-m3-f16", status(), pending).kind).toBe("enabled");
  });
});

describe("settling a pending request against recent[]", () => {
  const settled = status({
    recent: [{ request_id: REQ_B, state: "applied", reason: null, applied_at: "2026-09-13T12:00:01.000Z" }],
  });

  test("isRequestSettled is true for any terminal disposition, false while absent", () => {
    expect(isRequestSettled(settled, REQ_B)).toBe(true);
    expect(isRequestSettled(settled, REQ_A)).toBe(false);
    expect(isRequestSettled({ available: false, reason: "stale" }, REQ_B)).toBe(false);
    expect(isRequestSettled(null, REQ_B)).toBe(false);
  });

  test("settlePending clears exactly the dispositioned ids and keeps the rest", () => {
    const pending: PendingVisibilityMap = {
      "Qwen3.8-27B-UD-IQ3_S": { requestId: REQ_B, consumers: ["openwebui"] },
      "bge-m3-f16": { requestId: REQ_A, consumers: [] },
    };
    expect(settlePending(pending, settled)).toEqual({ "bge-m3-f16": { requestId: REQ_A, consumers: [] } });
  });

  test("an unavailable or unloaded status settles nothing — the pending mark survives", () => {
    const pending: PendingVisibilityMap = { "Qwen3.8-27B-UD-IQ3_S": { requestId: REQ_B, consumers: [] } };
    expect(settlePending(pending, { available: false, reason: "stale" })).toBe(pending);
    expect(settlePending(pending, null)).toBe(pending);
  });

  test("a rejected or failed disposition settles the chip too, and is readable as an outcome", () => {
    const rejected = status({
      recent: [
        { request_id: REQ_B, state: "rejected", reason: "cc requires a gateway id", applied_at: "2026-09-13T12:00:01.000Z" },
      ],
    });
    expect(settlePending({ k: { requestId: REQ_B, consumers: ["cc"] } }, rejected)).toEqual({});
    expect(requestOutcome(rejected, REQ_B)).toEqual({ state: "rejected", reason: "cc requires a gateway id" });
    expect(requestOutcome(rejected, REQ_A)).toBeNull();
    expect(requestOutcome(null, REQ_B)).toBeNull();
  });
});

describe("rowChips", () => {
  test("returns both chips in canonical order for one row", () => {
    const chips = rowChips("Qwen3.8-27B-UD-IQ3_S", status());
    expect(chips.map((chip) => chip.consumer)).toEqual(["cc", "openwebui"]);
    expect(chips.every((chip) => chip.kind === "enabled")).toBe(true);
  });

  test("on an unavailable status both chips disable with the same reason", () => {
    const chips = rowChips("Qwen3.8-27B-UD-IQ3_S", { available: false, reason: "disabled" });
    expect(chips.map((chip) => chip.disabled)).toEqual([true, true]);
    expect(new Set(chips.map((chip) => chip.reason)).size).toBe(1);
  });
});
