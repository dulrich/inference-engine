// Plan bench-dashboard-rust-port Decision 5 / WP-2: while zod exists (WP-2..WP-5), the Rust
// contract (crates/domain/src/contract/) agrees with packages/shared/src/*.ts on every exported
// schema. Corpus: (1) every `.parse`/`.safeParse` input of packages/shared/src/schema.test.ts
// (recorded by ./record-corpus.ts, step 1 of `bun run test:contract-agreement`); (2) every
// committed fixture body under apps/api/fixtures/** (json files, jsonl lines), fed to the
// schema the API/scripts read it with AND to every other schema; (3) structurally generated
// valid inputs and per-vocabulary mutations over the zod definitions; (4) hand cases.
//   (a) the generated TS runtime (`gen-ts`, emitted to target/contract-gen/index.ts — a scratch
//       path; packages/shared/src stays zod until WP-6): export-name parity, enum `.options`,
//       accept/reject, byte-identical parsed data, and on rejection the issue list minus
//       `message` (code, path and the code's own fields; message text may differ, Decision 4);
//   (b) the Rust validator (`validate`, via the `contract-check` binary) over every case of (a),
//       with the same comparison.
// Adapted from radar_tracker's retired WP-2 agreement test (fleet-tools d9ea0243).
import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import * as zod from "../../../packages/shared/src/index.ts";

const root = join(import.meta.dir, "../../..");
const genDir = join(root, "target/contract-gen");
const fixturesDir = join(root, "apps/api/fixtures");

function cargo(args: string[]) {
  const run = spawnSync("cargo", args, { cwd: root, encoding: "utf8", maxBuffer: 1 << 28 });
  if (run.status !== 0) throw new Error(`cargo ${args.join(" ")} failed (status ${run.status}):\n${run.stderr}`);
  return run.stdout;
}
const genSource = cargo(["run", "-q", "--locked", "-p", "bench-dashboard-domain", "--bin", "gen-ts"]);
mkdirSync(genDir, { recursive: true });
writeFileSync(join(genDir, "index.ts"), genSource);
const gen = (await import(join(genDir, "index.ts"))) as Record<string, unknown>;

type Result = { success: boolean; data?: unknown; error?: { message: string; issues: unknown[] } };
type Parser = { safeParse(input: unknown): Result };
type Def = { typeName: string; [key: string]: unknown };

const zodAll: Record<string, unknown> = { ...(zod as unknown as Record<string, unknown>) };
const isSchema = (v: unknown) => typeof (v as Parser | null)?.safeParse === "function";
const schemaNames = Object.keys(zodAll).filter((k) => isSchema(zodAll[k])).sort();
const constNames = Object.keys(zodAll).filter((k) => !isSchema(zodAll[k])).sort();
// Audit B1: the contract adds `ExperimentFullDetailSchema` (composed in apps/web/src/api.ts today).
const fullDetail = zod.ExperimentDetailSchema.extend({
  trials: zod.TrialRowSchema.array(),
  metrics: zod.MetricRowSchema.array(),
});
zodAll.ExperimentFullDetailSchema = fullDetail;
schemaNames.push("ExperimentFullDetailSchema");
schemaNames.sort();
const def = (s: unknown): Def => (s as { _def: Def })._def;

// ---- structural case generation over the zod definitions -----------------------------------
type Check = { kind: string; value?: number; inclusive?: boolean; regex?: RegExp };
const PATTERN_SAMPLES = [
  "2026-01-02",
  "req-20260101T000000Z-0123456789abcdef",
  "srv-20260101T000000Z-0123456789abcdef",
  "nightly-20260101",
  "abc-1",
];
const PATTERN_MISSES = ["2026-1-2", "２０２６-01-02", "req-20260101T000000Z-0123456789ABCDEF", "srv-x", "Abc", "-abc", "abc\n", "nightly-2026"];

function sample(s: unknown): unknown {
  const d = def(s);
  switch (d.typeName) {
    case "ZodString": {
      const re = (d.checks as Check[]).find((c) => c.kind === "regex")?.regex;
      return re ? PATTERN_SAMPLES.find((x) => re.test(x)) : "value";
    }
    case "ZodNumber": {
      const checks = d.checks as Check[];
      const min = checks.find((c) => c.kind === "min");
      const int = checks.some((c) => c.kind === "int");
      if (min) return min.inclusive ? min.value! + (int ? 0 : 0.5) : min.value! + 1;
      return int ? 3 : 2.5;
    }
    case "ZodBoolean":
      return true;
    case "ZodNull":
      return null;
    case "ZodUnknown":
      return { any: 1 };
    case "ZodLiteral":
      return d.value;
    case "ZodEnum":
      return (d.values as string[])[0];
    case "ZodArray":
      return [sample(d.type)];
    case "ZodObject": {
      const shape = (d.shape as () => Record<string, unknown>)();
      return Object.fromEntries(Object.entries(shape).map(([k, v]) => [k, sample(v)]));
    }
    case "ZodRecord":
      return { alpha: sample(d.valueType) };
    case "ZodNullable":
    case "ZodOptional":
    case "ZodDefault":
      return sample(d.innerType);
    case "ZodUnion":
    case "ZodDiscriminatedUnion":
      return sample((d.options as unknown[])[0]);
    default:
      throw new Error(`no sample for ${d.typeName}`);
  }
}

const STRINGS = ["", "   ", " has space ", "a\tb", "x".repeat(170), ...PATTERN_SAMPLES, ...PATTERN_MISSES];
const NUMBERS = [-1, 0, 0.05, 0.5, 1, 1.5, 3, 14, 15, 15.5, 480, 481, -0.5, 1e21, 2 ** 53];

// Alternative values for one position: valid variants and invalid ones.
function variants(s: unknown, depth: number): unknown[] {
  const d = def(s);
  const wrong = [null, 12345, "text", true, false, [], {}, "5", "", ["7"], [1, 2]];
  switch (d.typeName) {
    case "ZodString":
      return [...wrong, ...STRINGS];
    case "ZodNumber":
      return [...wrong, ...NUMBERS];
    case "ZodBoolean":
    case "ZodNull":
    case "ZodUnknown":
      return [...wrong, undefined];
    case "ZodLiteral":
      return [...wrong, d.value, undefined, 1, 2, "v1", "ok", "request", "progress", "slots", "none", "control-request-v1", "serving-status-v1"];
    case "ZodEnum":
      return [...wrong, ...(d.values as string[]), "not-a-member", "PENDING"];
    case "ZodArray": {
      const inner = variants(d.type, depth + 1).slice(0, depth > 1 ? 4 : 99);
      return [...wrong, [], [sample(d.type), sample(d.type)], ...inner.map((v) => [sample(d.type), v])];
    }
    case "ZodObject": {
      const shape = (d.shape as () => Record<string, unknown>)();
      const base = sample(s) as Record<string, unknown>;
      const out: unknown[] = [...wrong, { ...base, extraKey: 1 }, { extraKey: { nested: [1] }, ...base }, { ...base, b: 1, "10": 2, "2": 3 }];
      for (const [k, v] of Object.entries(shape)) {
        const { [k]: _omitted, ...rest } = base;
        out.push(rest);
        if (depth < 2) for (const alt of variants(v, depth + 1)) out.push({ ...base, [k]: alt });
        else out.push({ ...base, [k]: null }, { ...base, [k]: 12345 });
      }
      return out;
    }
    case "ZodRecord":
      return [
        ...wrong,
        { a: sample(d.valueType), b: null },
        { "": sample(d.valueType) },
        { "10": sample(d.valueType), "2": sample(d.valueType), b: sample(d.valueType) },
        { "1": 1, b: "x" },
        ...variants(d.valueType, depth + 1)
          .slice(0, 30)
          .map((v) => ({ ok: sample(d.valueType), bad: v })),
      ];
    case "ZodNullable":
    case "ZodOptional":
    case "ZodDefault":
      return [null, undefined, ...variants(d.innerType, depth)];
    case "ZodUnion":
    case "ZodDiscriminatedUnion": {
      const options = d.options as unknown[];
      const out: unknown[] = [...wrong];
      for (const o of options) out.push(sample(o), ...variants(o, depth));
      if (d.typeName === "ZodDiscriminatedUnion") {
        const key = d.discriminator as string;
        const base = sample(options[0]) as Record<string, unknown>;
        out.push({ ...base, [key]: "yes" }, { ...base, [key]: undefined }, { ...base, [key]: null }, { [key]: 1 });
      }
      return out;
    }
    default:
      throw new Error(`no variants for ${d.typeName}`);
  }
}

// ---- corpus (1): schema.test.ts inputs, recorded by ./record-corpus.ts ----------------------
const recordedPath = join(genDir, "schema-test-corpus.json");
if (!existsSync(recordedPath)) throw new Error(`${recordedPath} missing: run \`bun run test:contract-agreement\`, not this file alone`);
const RECORDED = JSON.parse(readFileSync(recordedPath, "utf8")) as [string, unknown][];
const unwrapUndefined = (v: unknown) => (JSON.stringify(v) === '{"$undefined":true}' ? undefined : v);

// ---- corpus (2): committed fixture bodies ---------------------------------------------------
function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}
const FIXTURE_BODIES: { file: string; body: unknown }[] = [];
for (const path of walk(fixturesDir).sort()) {
  const file = relative(fixturesDir, path);
  const text = readFileSync(path, "utf8");
  const parse = (raw: string) => {
    try {
      FIXTURE_BODIES.push({ file, body: JSON.parse(raw) as unknown });
    } catch {
      // malformed-* fixtures are deliberately not JSON; nothing to validate.
    }
  };
  if (file.endsWith(".jsonl")) {
    for (const line of text.split("\n")) if (line.trim()) parse(line);
  }
  else if (file.endsWith(".json")) parse(text);
}
/** The schema each fixture is read with (apps/api/src + scripts call sites). */
function fixtureSchemas(file: string, body: unknown): [string, unknown][] {
  const b = body as Record<string, unknown> | null;
  if (/control\/.*requests\/req-[^/]*\.json$/.test(file)) return [["ControlRequestFileSchema", body]];
  if (/serving\/contract-v1\/(control\/requests\/srv-|invalid\/request-)/.test(file)) return [["ServingRequestSchema", body]];
  if (/serving-status\.json$|invalid\/status-/.test(file)) {
    return [
      ["ServingStatusFileSchema", body],
      ["ServingStatusResponseSchema", { available: true, ...b }],
    ];
  }
  if (/visibility[^/]*\.json$/.test(file)) {
    const models = b?.models && typeof b.models === "object" ? Object.values(b.models as object) : [];
    return [
      ["ServingVisibilityDeclarationSchema", body],
      ["ServingVisibilityEnvelopeSchema", body],
      ["ServingVisibilityMapSchema", b?.models],
      ...models.map((m) => ["ServingVisibilityEntrySchema", m] as [string, unknown]),
    ];
  }
  if (/router\/logs\/router-/.test(file)) {
    return [
      ["RouterRequestRecordSchema", body],
      ["RouterProgressRecordSchema", body],
    ];
  }
  if (/router\/logs\/slots-/.test(file)) return [["RouterSlotsRecordSchema", body]];
  if (/router\/sidecar\//.test(file)) return [["RouterSidecarRecordSchema", body]];
  if (file.endsWith("config.json")) return [["WilsonSummarySchema", b?.wilson_summary ?? null]];
  return [];
}

// ---- corpus (4): hand cases per vocabulary kind ----------------------------------------------
const request = (over: Record<string, unknown>) => ({
  schema: "control-request-v1",
  request_id: "req-20260827T120000Z-00000000000000a1",
  action: "start",
  queue_stem: "night-1",
  minutes: 60,
  requested_at: "2026-08-27T12:00:00.000Z",
  ...over,
});
const unavailable = [{ available: false, reason: "disabled" }, { available: false, reason: "stale" }, { available: false }, { available: false, reason: "disabled", extra: 1 }, { available: true }, { available: "true" }, {}, { reason: "empty" }, null, []];
const HAND: Record<string, unknown[]> = {
  ControlStartRequestSchema: [{ queueStem: "a", minutes: 15 }, { queueStem: "A", minutes: 15 }, { queueStem: "a", minutes: 14.5 }, { queueStem: "-a", minutes: 481 }, { queueStem: "a\n", minutes: 60 }, {}],
  ControlCancelRequestSchema: [{ targetRequestId: "nightly-20260101" }, { targetRequestId: "req-20260827T120000Z-00000000000000a1" }, { targetRequestId: "" }],
  ControlRequestFileSchema: [
    request({}),
    request({ action: "cancel", queue_stem: undefined, minutes: undefined, target_request_id: "nightly-20260827" }),
    request({ target_request_id: "nightly-2026" }),
    request({ schema: "control-request-v2" }),
    request({ schema: undefined }),
    request({ requested_at: "" }),
    request({ minutes: 480.5, queue_stem: "Bad" }),
    request({ request_id: "srv-20260827T120000Z-00000000000000a1", extra: true }),
  ],
  ServingSourceSchema: [
    { url: "none", reason: "card silent" },
    { url: "none", reason: "card silent", verified: null },
    { url: "none", reason: "r", verified: "2026-01-02" },
    { url: "none", reason: "r", verified: "2026-1-2" },
    { url: "none" },
    { url: "https://x", verified: "2026-01-02" },
    { url: "https://x", verified: "2026-01-02", reason: null },
    { url: "https://x", verified: "yesterday" },
    { url: "https://x" },
    { url: 1, verified: "2026-01-02" },
    "none",
  ],
  ServingStatusResponseSchema: unavailable,
  RouterTelemetryResponseSchema: unavailable,
  RouterSlotsResponseSchema: unavailable,
  RouterSidecarResponseSchema: [...unavailable, { available: true, generatedAt: "t", file: "f", byKind: { http: 1.5, x: -1 }, personas: [], recentErrors: [] }],
  QueueListItemSchema: [{ name: "q", error: "bad" }, { name: "q", error: "bad", eta: null }, { name: "q", error: "bad", eta: 1 }, { name: "q" }, { name: 1, error: "x" }],
  ServingVisibilityRequestSchema: [{ modelKey: "", consumers: ["cc"] }, { modelKey: "m", consumers: ["cc", "openwebui", "x"] }, { modelKey: "€", consumers: [] }],
  ServingFlagsSchema: [{ contextLength: null, kvCacheTypeK: null, kvCacheTypeV: null, flashAttention: null, gpuLayers: null }],
  CaptureRecordSchema: [{ experimentId: "e", config: undefined, prompts: [], completions: [], samples: [] }],
  RouterUpstreamTimingsSchema: [{ draft_n: 3, x: 1.5 }, { draft_n: "3" }, { __proto__: 1 }],
};

const stringify = (v: unknown) => JSON.stringify(v) ?? "undefined";
/** An issue list minus `message` (recursively, through `unionErrors`), keys sorted. */
const shape = (issues: unknown): unknown =>
  Array.isArray(issues)
    ? issues.map(shape)
    : issues && typeof issues === "object"
      ? Object.fromEntries(
          Object.entries(issues as Record<string, unknown>)
            .filter(([k]) => k !== "message")
            .sort(([a], [b]) => (a < b ? -1 : 1))
            .map(([k, v]) => [k, shape(v)]),
        )
      : issues;

const BY_SCHEMA = new Map<string, unknown[]>(schemaNames.map((n) => [n, [sample(zodAll[n]), ...variants(zodAll[n], 0), ...(HAND[n] ?? [])]]));
for (const [name, input] of RECORDED) BY_SCHEMA.get(name)?.push(unwrapUndefined(input));
let fixtureCases = 0;
for (const { file, body } of FIXTURE_BODIES) {
  const mapped = fixtureSchemas(file, body);
  for (const [name, input] of mapped) BY_SCHEMA.get(name)?.push(input);
  // Every body is also an agreement probe for every other schema (nearly all rejections).
  for (const name of schemaNames) if (!mapped.some(([m]) => m === name)) BY_SCHEMA.get(name)?.push(body);
  fixtureCases += schemaNames.length + mapped.filter(([m]) => schemaNames.includes(m)).length;
}
const CASES: [string, unknown][] = schemaNames.flatMap((name) => (BY_SCHEMA.get(name) ?? []).map((input) => [name, input] as [string, unknown]));

function compare(name: string, input: unknown) {
  const z = (zodAll[name] as Parser).safeParse(input);
  const g = (gen[name] as Parser).safeParse(input);
  if (z.success !== g.success || (z.success && stringify(z.data) !== stringify(g.data))) {
    throw new Error(`${name} disagrees on ${stringify(input).slice(0, 400)}: zod ${z.success} ${stringify(z.data).slice(0, 300)} vs gen ${g.success} ${stringify(g.data).slice(0, 300)}`);
  }
  if (!g.success) {
    expect(typeof g.error?.message).toBe("string");
    const want = stringify(shape(JSON.parse(JSON.stringify(z.error?.issues))));
    const got = stringify(shape(JSON.parse(JSON.stringify(g.error?.issues))));
    if (got !== want) throw new Error(`${name} issues differ on ${stringify(input).slice(0, 300)}:\n gen ${got.slice(0, 600)}\n zod ${want.slice(0, 600)}`);
  }
  return z.success;
}

describe("(a) generated validators agree with zod", () => {
  test("export-name parity: every zod schema and constant is generated, and nothing more", () => {
    const generated = Object.keys(gen).sort();
    expect(generated).toEqual([...schemaNames, ...constNames].sort());
    expect(schemaNames.length).toBe(74);
  });

  test("constants are equal (regexes by source and flags)", () => {
    for (const name of constNames) {
      const z = zodAll[name];
      const g = gen[name];
      if (z instanceof RegExp) expect(`${g}`).toBe(`${z}`);
      else expect(g).toEqual(z);
    }
    expect(genSource).toContain('\nexport const CONTROL_REQUEST_SCHEMA_V1 = "control-request-v1";\n');
  });

  test("enum .options match", () => {
    for (const name of schemaNames) {
      if (def(zodAll[name]).typeName !== "ZodEnum") continue;
      expect((gen[name] as { options: unknown }).options).toEqual((zodAll[name] as { options: unknown }).options);
    }
  });

  test(".array() / .nullable() / .optional() agree", () => {
    const z = zod.RouterSidecarRecordSchema;
    const g = gen.RouterSidecarRecordSchema as typeof z;
    const rec = { ts: "t", kind: "http", persona: "p", extra: [1] };
    for (const input of [[rec], [{ ...rec, persona: 1 }], [{}], null, undefined, rec]) {
      expect(stringify(g.array().safeParse(input).data)).toBe(stringify(z.array().safeParse(input).data));
      expect(g.array().safeParse(input).success).toBe(z.array().safeParse(input).success);
      expect(g.nullable().safeParse(input).success).toBe(z.nullable().safeParse(input).success);
      expect(g.optional().safeParse(input).success).toBe(z.optional().safeParse(input).success);
    }
    expect(() => (gen.HealthResponseSchema as Parser & { parse(x: unknown): unknown }).parse({})).toThrow();
  });

  for (const name of schemaNames) {
    test(`${name}: accept/reject + byte-identical data + identical issue code/path/fields`, () => {
      const inputs = BY_SCHEMA.get(name) ?? [];
      let accepted = 0;
      for (const input of inputs) if (compare(name, input)) accepted += 1;
      expect(accepted).toBeGreaterThan(0);
      expect(inputs.length - accepted).toBeGreaterThan(0);
    });
  }
});

// ---- (b) the Rust validator (`validate`) against zod, over every case above ---------------
describe("(b) Rust validate agrees with zod", () => {
  test("all recorded cases", () => {
    cargo(["build", "-q", "--locked", "-p", "bench-dashboard-domain", "--bin", "contract-check"]);
    const run = spawnSync(join(root, "target/debug/contract-check"), [], {
      input: JSON.stringify(CASES.map(([name, input]) => [name, input === undefined ? { $undefined: true } : input])),
      encoding: "utf8",
      maxBuffer: 1 << 30,
    });
    expect(run.status).toBe(0);
    const results = JSON.parse(run.stdout) as [boolean | null, unknown, unknown][];
    expect(results.length).toBe(CASES.length);
    const mismatches: string[] = [];
    CASES.forEach(([name, input], i) => {
      // The Rust side reads JSON, which cannot carry a key holding `undefined`.
      const z = (zodAll[name] as Parser).safeParse(input === undefined ? undefined : JSON.parse(JSON.stringify(input)));
      const [ok, value, issues] = results[i]!;
      const want = z.success ? stringify(z.data) : stringify(shape(JSON.parse(JSON.stringify(z.error?.issues))));
      const got = ok ? stringify(value) : stringify(shape(issues));
      if (ok !== z.success || got !== want) mismatches.push(`${name} ${stringify(input).slice(0, 200)}: rust ${ok} ${got.slice(0, 400)} vs zod ${z.success} ${want.slice(0, 400)}`);
    });
    console.log(
      `contract agreement: ${schemaNames.length} schemas, ${CASES.length} cases (${RECORDED.length} from schema.test.ts, ${fixtureCases} from ${FIXTURE_BODIES.length} fixture bodies) replayed through Rust validate, ${mismatches.length} mismatches`,
    );
    expect(mismatches.slice(0, 10)).toEqual([]);
  }, 300_000);
});
