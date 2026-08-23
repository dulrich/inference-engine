import { buildTaskAlignedChartData, type TaskAlignedChartData } from "./chartData.ts";

/** The subset of `TrialRow` this module needs -- structural, not `TrialRow` itself, so a
 * test can build one without pulling in every unrelated field. */
export interface LongcontextTrialLike {
  task: string | null;
  promptTokens: number | null;
  classifierVerdict: string | null;
}

export interface TokenBinPoint {
  task: string;
  promptTokens: number;
  n: number;
  k: number;
  failureRate: number;
}

/** Groups `longcontext_probe` trials -- gathered client-side across every experiment that
 * has any (plan WP-2 bullet 3: "assemble client-side across experiments by grouping each
 * experiment's longcontext trials") -- by `(task, promptTokens)`: the real measured prompt-
 * token count at ingest time, distinct from the `position_fraction` bucket `GET
 * /api/curves/:id` already groups by server-side. There is no Wilson interval here -- that
 * endpoint exposes no per-token-count grouping -- just a plain k/n rate, the same "every
 * non-`correct` classifier_verdict counts as a failure" convention
 * `llama/bakeoff/longcontext.py`/`routes/curves.ts` both already use. A trial with a null
 * `task` or `promptTokens` (any non-longcontext sample type) is skipped -- callers should
 * pre-filter to `type === "longcontext_probe"`, but this stays defensive either way. */
export function groupByPromptTokens(trials: readonly LongcontextTrialLike[]): TokenBinPoint[] {
  const groups = new Map<string, { task: string; promptTokens: number; n: number; k: number }>();
  for (const trial of trials) {
    if (trial.task === null || trial.promptTokens === null) continue;
    const key = `${trial.task}::${trial.promptTokens}`;
    const group = groups.get(key) ?? { task: trial.task, promptTokens: trial.promptTokens, n: 0, k: 0 };
    group.n += 1;
    if (trial.classifierVerdict !== "correct") group.k += 1;
    groups.set(key, group);
  }
  return [...groups.values()]
    .map((g) => ({ ...g, failureRate: g.k / g.n }))
    .sort((a, b) => a.task.localeCompare(b.task) || a.promptTokens - b.promptTokens);
}

export function buildTokenBinChartData(bins: readonly TokenBinPoint[]): TaskAlignedChartData {
  return buildTaskAlignedChartData(bins.map((b) => ({ task: b.task, x: b.promptTokens, y: b.failureRate })));
}
