// Wilson score interval, mirroring `llama/bakeoff/longcontext.py wilson_interval(k, n,
// sided, confidence=0.95)`'s own contract (`llama/bakeoff/README.md` "Wilson intervals"):
// `sided` is REQUIRED, no default — the plan's binding "state the convention explicitly at
// every use site" pin. `"one"` returns only an upper bound (`lower: null`); `"two"` returns
// both. Only 95% is tabulated on either side; any other confidence throws rather than
// guessing a z-value.

const Z_TABLE: Record<"one" | "two", Record<number, number>> = {
  one: { 0.95: 1.6448536269514722 },
  two: { 0.95: 1.959963984540054 },
};

export interface WilsonInterval {
  sided: "one" | "two";
  confidence: number;
  lower: number | null;
  upper: number;
}

/** `k` failures out of `n` trials -> the Wilson score interval on the failure rate.
 * Verified against `llama/bakeoff/README.md`'s own cited example: `wilsonInterval(0, 30,
 * "one", 0.95).upper` === 0.0827... ("0/30 Wilson upper = 8.27%"). */
export function wilsonInterval(k: number, n: number, sided: "one" | "two", confidence = 0.95): WilsonInterval {
  const table = Z_TABLE[sided];
  const z = table[confidence];
  if (z === undefined) {
    throw new Error(`wilsonInterval: confidence ${confidence} is not tabulated for sided="${sided}"`);
  }
  if (n <= 0) throw new Error("wilsonInterval: n must be positive");

  const phat = k / n;
  const z2 = z * z;
  const denominator = 1 + z2 / n;
  const center = phat + z2 / (2 * n);
  const adjustment = z * Math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n));

  const upper = clamp01((center + adjustment) / denominator);
  if (sided === "one") return { sided, confidence, lower: null, upper };
  const lower = clamp01((center - adjustment) / denominator);
  return { sided, confidence, lower, upper };
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}
