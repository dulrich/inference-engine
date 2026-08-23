// Per-trial `classifier_verdict` display (Runs drill-down, plan WP-2 bullet 1). Verdicts
// are `llama/bakeoff/longcontext.py`'s own closed `VERDICTS = ("correct", "wrong",
// "degenerate", "truncated(benign)")` taxonomy -- mapped to the vendored Signalworks pill
// classes (CC-04: legal here because the pill's own uppercase label text IS the verdict,
// so the color is never the sole carrier). Any verdict this taxonomy hasn't seen (a future
// value, or a sample `type` with no verdict concept) falls back to `pill-muted` rather than
// throwing -- same "type is data, not an enum" discipline the ingest module itself follows.

const PILL_CLASS: Record<string, string> = {
  correct: "pill-ok",
  wrong: "pill-error",
  degenerate: "pill-error",
  "truncated(benign)": "pill-warn",
};

export function verdictPillClass(verdict: string | null): string {
  if (verdict === null) return "pill-muted";
  return PILL_CLASS[verdict] ?? "pill-muted";
}

export function verdictLabel(verdict: string | null): string {
  return verdict === null ? "n/a" : verdict;
}

/** Pulls `classifier_flags` off a trial's raw `samples.jsonl` line (present only on
 * `longcontext_probe` samples the classifier flagged as `degenerate` --
 * `llama/bakeoff/longcontext.py`'s `DEGENERATE_SHAPES` flags, e.g.
 * `max_ngram_repetition_rate`). There is no dedicated `TrialRow` column for it
 * (`packages/shared/src/trial.ts` keeps it out of the frozen projection on purpose) --
 * this is the one place the dashboard reaches into a trial's `raw` blob, per the brief's
 * own pin ("per-trial classifier_flags from the trial raw blob where present"). Returns
 * `null` when `raw` isn't an object, the key is absent, or it has no numeric entries --
 * callers render nothing for `null` rather than an empty flags chip. */
export function extractClassifierFlags(raw: unknown): Record<string, number> | null {
  if (typeof raw !== "object" || raw === null) return null;
  const flags = (raw as Record<string, unknown>).classifier_flags;
  if (typeof flags !== "object" || flags === null) return null;
  const entries = Object.entries(flags as Record<string, unknown>).filter(
    (entry): entry is [string, number] => typeof entry[1] === "number",
  );
  return entries.length === 0 ? null : Object.fromEntries(entries);
}

/** `"max_ngram_repetition_rate: 0.50"`-style inline summary for a table cell -- pure text,
 * no chip chrome (this isn't a status carrier, just a numeric readout of what the
 * classifier saw). */
export function formatClassifierFlags(flags: Record<string, number> | null): string {
  if (flags === null) return "";
  return Object.entries(flags)
    .map(([key, value]) => `${key}: ${value.toFixed(2)}`)
    .join(", ");
}
