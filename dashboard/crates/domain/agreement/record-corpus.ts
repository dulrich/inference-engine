// Preload for `bun run test:contract-agreement` step 1 (plan bench-dashboard-rust-port WP-2):
// while `packages/shared/src/schema.test.ts` runs, every `.parse`/`.safeParse` on an exported
// zod schema is recorded as `[schemaName, input]` (zod v3's `parse` calls `safeParse`), and the
// corpus is written to `target/contract-gen/schema-test-corpus.json` after the run for the agreement
// test (step 2) to replay. Inputs are snapshotted as JSON at call time (`{"$undefined": true}`
// for an absent input), so later mutation by a test cannot change them.
import { afterAll } from "bun:test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import * as shared from "../../../packages/shared/src/index.ts";

type Parser = { safeParse(input: unknown, params?: unknown): unknown };
const corpus: [string, unknown][] = [];
// zod v3 binds `parse`/`safeParse` per instance in the `ZodType` constructor, and the bound
// `parse` calls `this.safeParse`: wrapping each exported schema's own `safeParse` sees both.
for (const [name, value] of Object.entries(shared)) {
  const schema = value as Parser | null;
  if (typeof schema?.safeParse !== "function") continue;
  const original = schema.safeParse;
  schema.safeParse = (input: unknown, params?: unknown) => {
    corpus.push([name, input === undefined ? { $undefined: true } : JSON.parse(JSON.stringify(input))]);
    return original(input, params);
  };
}

const outDir = join(import.meta.dir, "../../../target/contract-gen");
// A preload-registered `afterAll` runs once, after every test file (`process.on("exit")` does
// not fire under `bun test`).
afterAll(() => {
  mkdirSync(outDir, { recursive: true });
  writeFileSync(join(outDir, "schema-test-corpus.json"), JSON.stringify(corpus));
});
