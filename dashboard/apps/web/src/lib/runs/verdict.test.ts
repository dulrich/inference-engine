import { describe, expect, test } from "bun:test";
import { extractClassifierFlags, formatClassifierFlags, verdictLabel, verdictPillClass } from "./verdict.ts";

describe("verdictPillClass", () => {
  test("maps the closed VERDICTS taxonomy to pill classes", () => {
    expect(verdictPillClass("correct")).toBe("pill-ok");
    expect(verdictPillClass("wrong")).toBe("pill-error");
    expect(verdictPillClass("degenerate")).toBe("pill-error");
    expect(verdictPillClass("truncated(benign)")).toBe("pill-warn");
  });

  test("null (no verdict concept for this sample type) is muted", () => {
    expect(verdictPillClass(null)).toBe("pill-muted");
  });

  test("an unrecognized future verdict falls back to muted rather than throwing", () => {
    expect(verdictPillClass("some_future_verdict")).toBe("pill-muted");
  });
});

describe("verdictLabel", () => {
  test("null reads as n/a", () => {
    expect(verdictLabel(null)).toBe("n/a");
  });

  test("a real verdict passes through", () => {
    expect(verdictLabel("wrong")).toBe("wrong");
  });
});

describe("extractClassifierFlags", () => {
  test("pulls numeric classifier_flags off a raw longcontext_probe line", () => {
    const raw = {
      type: "longcontext_probe",
      classifier_verdict: "degenerate",
      classifier_shape: "repetition",
      classifier_flags: { max_ngram_repetition_rate: 0.5 },
    };
    expect(extractClassifierFlags(raw)).toEqual({ max_ngram_repetition_rate: 0.5 });
  });

  test("an empty classifier_flags object is null, not an empty object", () => {
    expect(extractClassifierFlags({ classifier_flags: {} })).toBeNull();
  });

  test("a non-object raw is null", () => {
    expect(extractClassifierFlags(null)).toBeNull();
    expect(extractClassifierFlags("not an object")).toBeNull();
    expect(extractClassifierFlags(undefined)).toBeNull();
  });

  test("a raw blob with no classifier_flags key is null", () => {
    expect(extractClassifierFlags({ type: "sampling_echo_check" })).toBeNull();
  });

  test("non-numeric entries are filtered out; an all-non-numeric object is null", () => {
    expect(extractClassifierFlags({ classifier_flags: { note: "not numeric", rate: 0.2 } })).toEqual({ rate: 0.2 });
    expect(extractClassifierFlags({ classifier_flags: { note: "not numeric" } })).toBeNull();
  });
});

describe("formatClassifierFlags", () => {
  test("null renders as an empty string", () => {
    expect(formatClassifierFlags(null)).toBe("");
  });

  test("formats key: value pairs, two-decimal", () => {
    expect(formatClassifierFlags({ max_ngram_repetition_rate: 0.5, another_flag: 1 })).toBe(
      "max_ngram_repetition_rate: 0.50, another_flag: 1.00",
    );
  });
});
