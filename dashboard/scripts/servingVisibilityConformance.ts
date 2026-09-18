// The visibility/gateway conformance policy (WP-3, `plans/bench-dashboard-serving-control.md`;
// split out of `check-serving-recipes.ts` by review finding F7 — modularity, alongside F4 —
// boundary-type-contracts). Checks the VISIBILITY DECLARATION (`router/visibility.json`)
// against the served config in the direction the config owns -- every served key declared,
// the closed `consumers` vocabulary in canonical order, `gateway_id` either null or one of
// that key's own aliases and unique across the map, `cc` implying a non-null `gateway_id`,
// and the `max-tokens-crosswalk` between each declared `gateway_id`'s `max_output.answer_tokens`
// and `claude-local.maxOutputTokens` in `router/gateway.config.json` (the ceiling router patch
// 0010 clamps to). The other direction -- every gateway model id declared -- is
// `router/model-picker-lineup.mjs --check`'s half of the same gate.
//
// Pure function of an already-parsed alias list plus the two companion file bodies -- no I/O
// of its own. `check-serving-recipes.ts` is the only caller: it parses `llama/config.yaml`
// once and reads the two companion files once, and hands all three to this module and to
// `servingRecipeConformance.ts`.
//
// F4's fix: the schema literal, the declaration envelope, the per-entry shape and the canonical
// consumer order are all imported from `packages/shared/src/servingRequest.ts` -- never
// redeclared here. The former hand-written duck-typed parser and its own `CONSUMER_VOCABULARY`
// literal are gone; `ServingVisibilityEnvelopeSchema`/`ServingVisibilityEntrySchema` are the one
// shape source and `SERVING_CONSUMER_ORDER` the one order/vocabulary source, same as the POST
// body builder and the fixtures.
//
// The envelope is parsed once for the document and each ENTRY is parsed separately, on purpose:
// a gate whose job is to name every non-conforming entry must not let one off-vocabulary
// `consumers` token collapse five other entries' diagnostics into a single whole-document
// `visibility-unreadable`. Document-level facts (schema literal, `models` is a map) fail the
// whole file because nothing else is checkable without them; entry-level facts do not.
import { dirname, join } from "node:path";
import {
  SERVING_CONSUMER_ORDER,
  SERVING_VISIBILITY_SCHEMA_V1,
  ServingVisibilityEntrySchema,
  ServingVisibilityEnvelopeSchema,
  type ServingConsumer,
  type ServingVisibilityEntry,
  type ServingVisibilityEnvelope,
} from "../packages/shared/src/servingRequest.ts";
import type { ServingAlias } from "../packages/shared/src/serving.ts";
import { formatFinding, type RecipeFinding } from "./servingRecipeConformance.ts";

export { formatFinding, type RecipeFinding };

/** The visibility declaration and the gateway config, resolved RELATIVE TO THE CONFIG so a
 * fixture run and the real run resolve their own siblings, never each other's. Both are
 * overridable (`--visibility` / `--gateway`), which is how the fixtures carry their own. */
export function resolveVisibilityPath(configPath: string): string {
  return join(dirname(configPath), "../router/visibility.json");
}
export function resolveGatewayPath(configPath: string): string {
  return join(dirname(configPath), "../router/gateway.config.json");
}

/** The gateway provider whose `maxOutputTokens` map patch 0010's clamp reads. */
const CLAMP_PROVIDER = "claude-local";

type VisibilityModels = ServingVisibilityEnvelope["models"];

/** The parsed declaration, or a `problem` string naming why it is unusable. Fail-closed: an
 * unreadable/malformed declaration is a finding, never "no visibility rules apply" — that
 * silence is exactly how a declaration could rot unnoticed while both generators read it.
 * Parses with `ServingVisibilityEnvelopeSchema` (F4): ONLY a document-level shape or literal
 * mismatch fails the whole file here. Entry-level type violations, canonical order, alias
 * membership, `cc`-requires-gateway, duplicate-gateway-id and the max-tokens crosswalk are all
 * per-entry checks below, so each reports on its own key. */
function parseVisibility(
  visibilityText: string | null,
): { models: VisibilityModels | null; problem: string | null } {
  if (visibilityText === null) {
    return { models: null, problem: "the visibility declaration is missing or unreadable" };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(visibilityText);
  } catch (err) {
    return { models: null, problem: `the visibility declaration is not valid JSON: ${(err as Error).message}` };
  }
  const result = ServingVisibilityEnvelopeSchema.safeParse(parsed);
  if (!result.success) {
    const issue = result.error.issues[0];
    const path = issue !== undefined && issue.path.length > 0 ? ` at \`${issue.path.join(".")}\`` : "";
    const detail = issue === undefined ? "does not match" : issue.message;
    return {
      models: null,
      problem: `the visibility declaration does not match the \`${SERVING_VISIBILITY_SCHEMA_V1}\` schema${path}: ${detail}`,
    };
  }
  return { models: result.data.models, problem: null };
}

/** `claude-local.maxOutputTokens` off `router/gateway.config.json`, or `null` when the file is
 * missing/malformed or the provider carries no map. `null` makes every crosswalk check report,
 * which is the fail-closed direction. */
function parseGatewayCaps(gatewayText: string | null): Record<string, unknown> | null {
  if (gatewayText === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(gatewayText);
  } catch {
    return null;
  }
  const providers = (parsed as { providers?: unknown })?.providers;
  if (!Array.isArray(providers)) return null;
  const provider = providers.find(
    (p) => (p as { name?: unknown })?.name === CLAMP_PROVIDER,
  ) as { maxOutputTokens?: unknown } | undefined;
  const caps = provider?.maxOutputTokens;
  if (caps === null || typeof caps !== "object" || Array.isArray(caps)) return null;
  return caps as Record<string, unknown>;
}

/** `consumers` must be deduplicated and in `SERVING_CONSUMER_ORDER` (imported, never
 * redeclared — F4). The schema already guarantees every element is a member of the closed
 * vocabulary; this is the one thing it can't express, since it needs the whole array, not one
 * element at a time. */
function consumersOrderProblem(consumers: readonly ServingConsumer[]): string | null {
  const canonical = SERVING_CONSUMER_ORDER.filter((c) => consumers.includes(c));
  if (consumers.length !== canonical.length || canonical.some((c, i) => consumers[i] !== c)) {
    return `\`consumers\` ${JSON.stringify(consumers)} is duplicated or out of canonical order — expected ${JSON.stringify(canonical)}`;
  }
  return null;
}

/** The visibility rules for ONE served key — EVERY served key, embed/rerank included, unlike
 * the recipe rules in `servingRecipeConformance.ts`. At most one finding per key, in the
 * precedence order below, so the "one line per non-conforming entry" contract holds here too.
 * `claimed` carries the gateway ids already seen, which is how the uniqueness rule reports the
 * SECOND claimant. */
function checkVisibilityEntry(
  alias: ServingAlias,
  models: VisibilityModels,
  caps: Record<string, unknown> | null,
  claimed: Map<string, string>,
  findings: RecipeFinding[],
): void {
  const modelKey = alias.modelKey;
  const push = (rule: string, detail: string): void => {
    findings.push({ modelKey, rule, detail });
  };

  const raw = models[modelKey];
  if (raw === undefined) {
    push("visibility-missing", "no entry in `router/visibility.json` — every served key must be declared there");
    return;
  }

  // The entry's own TYPES, checked against the shared schema here rather than in the document
  // parse, so a single bad entry reports as itself instead of blinding the rest of the file.
  // The issue path picks the rule, which is how the two pre-split diagnostics survive the F4
  // rewrite: `consumers` problems stay `visibility-vocabulary`, `gateway_id` problems stay
  // `visibility-gateway-id`.
  const parsedEntry = ServingVisibilityEntrySchema.safeParse(raw);
  if (!parsedEntry.success) {
    const issue = parsedEntry.error.issues[0];
    const field = issue?.path[0];
    if (field === "consumers") {
      const consumers = (raw as { consumers?: unknown })?.consumers;
      push(
        "visibility-vocabulary",
        Array.isArray(consumers)
          ? `\`consumers\` carries ${consumers
              .filter((c) => !SERVING_CONSUMER_ORDER.includes(c as ServingConsumer))
              .map((u) => JSON.stringify(u))
              .join(", ")} — the closed vocabulary is ${SERVING_CONSUMER_ORDER.join(", ")}`
          : "`consumers` is not an array",
      );
      return;
    }
    const detail = issue === undefined ? "does not match the declared entry shape" : issue.message;
    push(
      "visibility-gateway-id",
      field === "gateway_id"
        ? `\`gateway_id\` ${JSON.stringify((raw as { gateway_id?: unknown })?.gateway_id ?? null)} is neither null nor a string`
        : `entry does not match the \`${SERVING_VISIBILITY_SCHEMA_V1}\` entry shape: ${detail}`,
    );
    return;
  }
  const entry: ServingVisibilityEntry = parsedEntry.data;

  const orderProblem = consumersOrderProblem(entry.consumers);
  if (orderProblem !== null) {
    push("visibility-vocabulary", orderProblem);
    return;
  }
  const consumers = entry.consumers;

  const gatewayId = entry.gateway_id;
  if (gatewayId !== null && !alias.aliases.includes(gatewayId)) {
    push("visibility-gateway-id", `\`gateway_id\` "${gatewayId}" is not one of this key's aliases (${alias.aliases.join(", ") || "none"})`);
    return;
  }
  if (gatewayId === null && consumers.includes("cc")) {
    push("visibility-cc-requires-gateway-id", "`consumers` includes `cc` but `gateway_id` is null — the Claude Code picker can only emit a gateway id");
    return;
  }
  if (gatewayId !== null) {
    const owner = claimed.get(gatewayId);
    if (owner !== undefined) {
      push("visibility-duplicate-gateway-id", `\`gateway_id\` "${gatewayId}" is already claimed by ${owner} — one picker row cannot answer to two declarations`);
      return;
    }
    claimed.set(gatewayId, modelKey);

    // The crosswalk patch 0010's clamp depends on (item 8c): the gateway's declared ceiling for
    // this id must BE this key's advisory `max_output.answer_tokens`, not merely near it.
    const declared = alias.capability?.maxOutput?.answerTokens ?? null;
    const cap = caps === null ? undefined : caps[gatewayId];
    if (cap === undefined) {
      push("max-tokens-crosswalk", `\`${CLAMP_PROVIDER}.maxOutputTokens["${gatewayId}"]\` is missing from \`router/gateway.config.json\` — patch 0010's clamp reads that map`);
    } else if (declared === null) {
      push("max-tokens-crosswalk", `\`${CLAMP_PROVIDER}.maxOutputTokens["${gatewayId}"]\` is ${String(cap)} but this key declares no \`max_output.answer_tokens\` to crosswalk it against`);
    } else if (cap !== declared) {
      push("max-tokens-crosswalk", `\`${CLAMP_PROVIDER}.maxOutputTokens["${gatewayId}"]\` ${String(cap)} ≠ \`max_output.answer_tokens\` ${declared}`);
    }
  }
}

/** Every visibility/gateway non-conformance across `aliases`, in list order — EVERY served
 * key, embed and rerank included, is exactly the entries whose `[]` declaration is the point.
 * A missing/malformed declaration is one finding for the whole file (`router/visibility.json`
 * as the "model key") rather than silence, and per-entry checks are skipped for that pass —
 * there is nothing to check them against. */
export function checkVisibilityConformance(
  aliases: readonly ServingAlias[],
  visibilityText: string | null,
  gatewayText: string | null,
): RecipeFinding[] {
  const findings: RecipeFinding[] = [];
  const { models, problem } = parseVisibility(visibilityText);
  if (models === null) {
    findings.push({ modelKey: "router/visibility.json", rule: "visibility-unreadable", detail: problem ?? "unknown" });
    return findings;
  }
  const caps = parseGatewayCaps(gatewayText);
  const claimed = new Map<string, string>();
  for (const alias of aliases) {
    checkVisibilityEntry(alias, models, caps, claimed, findings);
  }
  return findings;
}
