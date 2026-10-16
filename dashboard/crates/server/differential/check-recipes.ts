// `bun run test:recipes-differential` (plan bench-dashboard-rust-port WP-5, Decision 7): runs
// the TS gate (`bun scripts/check-serving-recipes.ts`) and the Rust binary (`check-recipes`)
// over the repo's real `llama/config.yaml` + `router/visibility.json` +
// `router/gateway.config.json`, the committed fixtures, and mutated scratch copies covering
// every finding kind either policy can emit (plus a missing config, unparseable YAML and the
// TS's TypeError paths). Asserts identical stdout and exit code per case; stderr must match
// too, except on the cases marked `runtime` (an uncaught error: Bun prints its own exception
// text, the Rust binary `error: <message>` — the 2026-10-15 runtime-text allowance).
// Removed with the TS scripts in WP-6. Scratch copies live under the OS temp dir only.
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const DASH = join(import.meta.dir, "../../..");
const REPO = join(DASH, "..");
const REAL = {
  config: join(REPO, "llama/config.yaml"),
  visibility: join(REPO, "router/visibility.json"),
  gateway: join(REPO, "router/gateway.config.json"),
};
const FIX = join(DASH, "apps/api/fixtures/serving");

const build = Bun.spawnSync(["cargo", "build", "-q", "--locked", "-p", "bench-dashboard-server", "--bin", "check-recipes"], { cwd: DASH, stdout: "inherit", stderr: "inherit" });
if (build.exitCode !== 0) process.exit(build.exitCode ?? 1);
const RUST = join(DASH, "target/debug/check-recipes");
const TS = join(DASH, "scripts/check-serving-recipes.ts");

// Scratch mutation of parsed documents by deep property paths; the shape is each case's own
// business, and the runtime comparison (not the type system) is what this script checks.
// oxlint-disable-next-line typescript/no-explicit-any
type Json = any;
const baseConfig: Json = Bun.YAML.parse(await Bun.file(REAL.config).text());
const baseVisibility: Json = JSON.parse(await Bun.file(REAL.visibility).text());
const baseGateway: Json = JSON.parse(await Bun.file(REAL.gateway).text());

const N = "nemotron-3-nano-4B-UD-Q4_K_XL"; // chat, gateway_id nemotron-4b
const Q = "Qwen3.6-35B-A3B-UD-Q4_K_XL"; // chat, gateway-capped
const L = "Ling-3.0-tiny-UD-Q4_K_XL"; // chat, gateway_id null
for (const k of [N, Q, L]) if (!(k in baseConfig.models)) throw new Error(`differential anchor ${k} is gone from llama/config.yaml`);

interface Files { config: Json; visibility: Json; gateway: Json }
interface Case {
  name: string;
  /** Mutates parsed copies; written back as JSON (valid YAML for the config). */
  mutate?: (f: Files) => void;
  /** Raw file bodies (null = file absent) instead of a mutation. */
  raw?: { config?: string | null; visibility?: string | null; gateway?: string | null };
  /** Explicit argv; `{dir}` is the scratch dir. Default: `--config {dir}/llama/config.yaml`. */
  args?: string[];
  runtime?: boolean;
}

const md = (f: Files, k = N): Json => f.config.models[k].metadata;
const setCmd = (f: Files, k: string, fn: (c: string) => string): void => {
  f.config.models[k].cmd = fn(f.config.models[k].cmd);
};

const cases: Case[] = [
  { name: "real files, default path (no args)", args: [] },
  { name: "real files, explicit flags", args: ["--config", REAL.config, "--visibility", REAL.visibility, "--gateway", REAL.gateway] },
  { name: "real files, inline flags", args: [`--config=${REAL.config}`, `--visibility=${REAL.visibility}`, `--gateway=${REAL.gateway}`] },
  { name: "fixture conforming", args: ["--config", join(FIX, "conforming.yaml"), "--visibility", join(FIX, "conforming-visibility.json"), "--gateway", join(FIX, "conforming-gateway.json")] },
  { name: "fixture non-conforming", args: ["--config", join(FIX, "non-conforming.yaml"), "--visibility", join(FIX, "non-conforming-visibility.json"), "--gateway", join(FIX, "non-conforming-gateway.json")] },
  { name: "fixture config, companions resolved off the config (absent)", args: ["--config", join(FIX, "conforming.yaml")] },
  { name: "unmutated scratch copy (JSON-as-YAML)" },
  // ---- recipe policy ----
  { name: "missing-key: no metadata", mutate: (f) => { delete f.config.models[N].metadata; } },
  { name: "missing-key: keys", mutate: (f) => { delete md(f).serving.mtp; delete md(f).tool_calling; md(f).input_modalities = []; delete md(f).max_output; } },
  { name: "sampling-undeclared + sources-keys missing", mutate: (f) => { delete md(f).sources["sampling.top_k"]; } },
  { name: "sampling-undeclared: gap not none", mutate: (f) => { md(f).sources["sampling.min_p"] = { url: "https://huggingface.co/x", verified: "2026-01-01" }; } },
  { name: "sources-missing", mutate: (f) => { delete md(f).sources; } },
  { name: "sources-keys: unexpected", mutate: (f) => { md(f).sources.extra = { url: "none", reason: "r" }; } },
  { name: "sources as string / array", mutate: (f) => { md(f).sources = "ab"; md(f, Q).sources = [{ url: "none" }]; } },
  { name: "source-shape", mutate: (f) => { md(f).sources["sampling.top_k"] = "x"; md(f).sources["sampling.min_p"] = null; md(f).sources["sampling.presence_penalty"] = 3; } },
  { name: "source-url missing", mutate: (f) => { md(f).sources["sampling.temperature"] = { verified: "2026-01-01" }; md(f).sources["sampling.top_p"] = { url: "" }; md(f).sources["sampling.top_k"] = [1]; } },
  { name: "source-url unapproved", mutate: (f) => {
      const s = md(f).sources;
      s["sampling.temperature"].url = "https://example.com/x";
      s["sampling.top_p"].url = "http://huggingface.co/x";
      s["max_output.answer_tokens"].url = "https://huggingface.co/";
      md(f, Q).sources["sampling.temperature"].url = "not a url";
      md(f, Q).sources["sampling.top_p"].url = "https://HuggingFace.co:443/a/../";
    } },
  { name: "source-url approved variants", mutate: (f) => {
      md(f).sources["sampling.temperature"].url = "https://www.HuggingFace.co/a?b#c";
      md(f).sources["sampling.top_p"].url = "  https://docs.sglang.ai/x  ";
      md(f).sources["max_output.answer_tokens"].url = "https://user:pw@docs.vllm.ai:8443/x";
    } },
  { name: "source-reason", mutate: (f) => { md(f).sources["sampling.top_k"] = { url: "none" }; md(f).sources["sampling.min_p"] = { url: "none", reason: "  \n" }; md(f).sources["sampling.presence_penalty"] = { url: "none", reason: 5 }; } },
  { name: "source-verified", mutate: (f) => {
      md(f).sources["sampling.temperature"].verified = "2026/09/14";
      delete md(f).sources["sampling.top_p"].verified;
      md(f).sources["max_output.answer_tokens"].verified = 20260914;
      md(f, Q).sources["sampling.temperature"] = { url: "https://huggingface.co/a", verified: "2026-09-14\n" };
    } },
  { name: "projection: context_length", mutate: (f) => { md(f).context_length = 65536; md(f, Q).context_length = "131072"; } },
  { name: "projection: no -c", mutate: (f) => { setCmd(f, N, (c) => c.replace(/-c \d+/, "")); } },
  { name: "projection: reasoning budget", mutate: (f) => {
      md(f).max_output.reasoning_budget = 4096;
      md(f).serving.reasoning_budget = 0;
      setCmd(f, Q, (c) => `${c} --reasoning-budget 0`);
      setCmd(f, L, (c) => `${c} --reasoning-budget x`);
      md(f, L).serving.reasoning_budget = 7;
    } },
  { name: "projection: optional serving keys", mutate: (f) => {
      md(f).serving.ubatch = 512;
      setCmd(f, N, (c) => `${c} -ncmoe 4`);
      md(f, Q).serving.reasoning_mode = "off";
      setCmd(f, L, (c) => `${c} -ub 256 --reasoning`);
      md(f, L).serving.ubatch = 512;
      md(f, L).serving.reasoning_mode = "on";
    } },
  { name: "projection: optional key value-less / non-int", mutate: (f) => { setCmd(f, N, (c) => `${c} -ub abc`); md(f).serving.ubatch = 12; setCmd(f, Q, (c) => `${c} -ncmoe`); md(f, Q).serving.moe_cpu_offload_layers = "x"; } },
  { name: "projection: kv cache", mutate: (f) => {
      md(f).serving.kv_cache_type = "q8_0";
      setCmd(f, Q, (c) => `${c} -ctv q4_0`);
      md(f, Q).serving.kv_cache_type = 8;
      setCmd(f, L, (c) => `${c} -ctk q8_0`);
    } },
  { name: "projection: flash attention + quant", mutate: (f) => {
      md(f).serving.flash_attention = true;
      md(f, Q).serving.flash_attention = "yes";
      md(f, Q).serving.quant = "Q9_K";
      setCmd(f, L, (c) => c.replace(/-m \S+/, ""));
    } },
  { name: "projection + vocabulary: modalities, reasoning", mutate: (f) => {
      md(f).input_modalities = ["text", "image"];
      md(f, Q).input_modalities = ["text", "audio", null];
      md(f, Q).output_modalities = ["text", "speech"];
      md(f, L).reasoning = "maybe";
    } },
  { name: "answer-cap", mutate: (f) => {
      md(f).max_output.answer_tokens = 0;
      md(f, Q).max_output.answer_tokens = 1.5;
      md(f, L).max_output.answer_tokens = "big";
      md(f, "Ling-3.0-tiny-UD-Q6_K_XL").max_output.answer_tokens = 64000;
      const g = "gemma-4-12B-it-qat-UD-Q4_K_XL";
      md(f, g).context_length = 1000;
      setCmd(f, g, (c) => c.replace(/-c \d+/, "-c 1000"));
      md(f, g).max_output.answer_tokens = 2000;
    } },
  { name: "non-chat skip by output modality", mutate: (f) => { md(f).output_modalities = ["embedding"]; delete md(f).sources; } },
  { name: "string output_modalities, no \"text\" substring: skipped as non-chat", mutate: (f) => { md(f).output_modalities = "embedding"; delete md(f).sources; } },
  { name: "TypeError: string output_modalities with a \"text\" substring", mutate: (f) => { md(f, Q).output_modalities = "text,score"; }, runtime: true },
  // ---- visibility policy ----
  { name: "visibility-unreadable: absent", raw: { visibility: null } },
  { name: "visibility-unreadable: invalid JSON", raw: { visibility: "{not json" } },
  { name: "visibility-unreadable: empty", raw: { visibility: "" } },
  { name: "visibility-unreadable: wrong schema", raw: { visibility: '{"schema":"serving-visibility-v2","models":{}}' } },
  { name: "visibility-unreadable: array", raw: { visibility: "[]" } },
  { name: "visibility-unreadable: models not a record", raw: { visibility: '{"schema":"serving-visibility-v1","models":[]}' } },
  { name: "visibility-unreadable: no models", raw: { visibility: '{"schema":"serving-visibility-v1"}' } },
  { name: "visibility-missing", mutate: (f) => { delete f.visibility.models[N]; delete f.visibility.models["bge-m3-f16"]; } },
  { name: "visibility-vocabulary", mutate: (f) => {
      f.visibility.models[N].consumers = ["crucible", "openwebui", 3];
      f.visibility.models[Q].consumers = "cc";
      f.visibility.models[L].consumers = ["openwebui", "cc"];
      f.visibility.models["bge-m3-f16"].consumers = ["cc", "cc"];
    } },
  { name: "visibility-gateway-id", mutate: (f) => {
      f.visibility.models[N].gateway_id = "nope";
      f.visibility.models[Q].gateway_id = 42;
      f.visibility.models[L] = null;
      f.visibility.models["bge-m3-f16"] = { consumers: [] };
      f.visibility.models["bge-small-en-v1.5-f16"] = "x";
      f.visibility.models["bge-reranker-v2-m3-Q8_0"] = { gateway_id: null };
    } },
  { name: "visibility-gateway-id: alias list empty-joined", mutate: (f) => { f.config.models[N].aliases = [null]; } },
  { name: "visibility-cc-requires-gateway-id", mutate: (f) => { f.visibility.models[L].consumers = ["cc", "openwebui"]; } },
  { name: "visibility-duplicate-gateway-id", mutate: (f) => {
      f.config.models[L].aliases.push("nemotron-4b");
      f.visibility.models[L].gateway_id = "nemotron-4b";
    } },
  { name: "max-tokens-crosswalk", mutate: (f) => {
      const caps = f.gateway.providers.find((p: Json) => p.name === "claude-local").maxOutputTokens;
      delete caps["qwen36-a3b"];
      caps["nemotron-4b"] = 1;
      caps["qwen38-27b-iq3s"] = "32000";
      delete md(f, "Qwen3.8-27B-UD-IQ3_S").max_output;
    } },
  { name: "max-tokens-crosswalk: no providers", raw: { gateway: '{"providers":[]}' } },
  { name: "max-tokens-crosswalk: gateway invalid JSON", raw: { gateway: "{" } },
  { name: "max-tokens-crosswalk: gateway absent", raw: { gateway: null } },
  { name: "max-tokens-crosswalk: caps is an array", mutate: (f) => { f.gateway.providers.find((p: Json) => p.name === "claude-local").maxOutputTokens = []; } },
  { name: "grouped: both policies on one key", mutate: (f) => { delete f.visibility.models[N]; delete md(f).serving.mtp; } },
  // ---- error paths ----
  { name: "missing config", raw: { config: null }, runtime: true },
  { name: "unparseable YAML", raw: { config: "models:\n  a: [1, 2\n" }, runtime: true },
  { name: "TypeError: string aliases not matching gateway_id", mutate: (f) => { f.config.models[N].aliases = "zzz"; }, runtime: true },
  { name: "TypeError: string input_modalities", mutate: (f) => { md(f).input_modalities = "text"; }, runtime: true },
  { name: "string aliases matching gateway_id (substring)", mutate: (f) => { f.config.models[N].aliases = "x-nemotron-4b-y"; } },
];

function run(cmd: string[]): { stdout: string; stderr: string; code: number } {
  const r = Bun.spawnSync(cmd, { cwd: tmpdir(), stdout: "pipe", stderr: "pipe" });
  return { stdout: r.stdout.toString(), stderr: r.stderr.toString(), code: r.exitCode ?? -1 };
}

const scratch = mkdtempSync(join(tmpdir(), "check-recipes-differential-"));
let failures = 0;
let stderrDiffs = 0;
const rules = new Set<string>();
try {
  cases.forEach((c, i) => {
    const dir = join(scratch, String(i));
    mkdirSync(join(dir, "llama"), { recursive: true });
    mkdirSync(join(dir, "router"), { recursive: true });
    const files: Files = structuredClone({ config: baseConfig, visibility: baseVisibility, gateway: baseGateway });
    c.mutate?.(files);
    const write = (p: string, body: string | null | undefined, fallback: string): void => {
      const text = body === undefined ? fallback : body;
      if (text !== null) writeFileSync(join(dir, p), text);
    };
    write("llama/config.yaml", c.raw?.config, JSON.stringify(files.config, null, 2));
    write("router/visibility.json", c.raw?.visibility, JSON.stringify(files.visibility, null, 2));
    write("router/gateway.config.json", c.raw?.gateway, JSON.stringify(files.gateway, null, 2));
    const args = c.args ?? ["--config", join(dir, "llama/config.yaml")];
    const ts = run(["bun", TS, ...args]);
    const rs = run([RUST, ...args]);
    for (const line of ts.stdout.split("\n")) {
      const m = /^[^ ]+: ([a-z-]+) — /.exec(line);
      if (m?.[1] !== undefined) rules.add(m[1]);
    }
    const problems: string[] = [];
    if (ts.stdout !== rs.stdout) problems.push(`stdout differs\n--- ts\n${ts.stdout}--- rust\n${rs.stdout}`);
    if (ts.code !== rs.code) problems.push(`exit ${ts.code} (ts) vs ${rs.code} (rust)`);
    if (ts.stderr !== rs.stderr) {
      if (c.runtime === true && ts.code !== 0 && ts.stdout === "") stderrDiffs++;
      else problems.push(`stderr differs\n--- ts\n${ts.stderr}--- rust\n${rs.stderr}`);
    }
    const status = problems.length === 0 ? "ok  " : "FAIL";
    console.log(`${status} ${c.name} (exit ${ts.code}, ${ts.stdout.split("\n").length - 1} lines)`);
    if (problems.length > 0) {
      failures++;
      for (const p of problems) console.log(`     ${p.replaceAll("\n", "\n     ")}`);
    }
    if (process.env.DIFF_VERBOSE && c.runtime) console.log(`     ts stderr: ${ts.stderr.trim().split("\n")[0]}\n     rust stderr: ${rs.stderr.trim()}`);
  });
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
console.log(`${cases.length} cases, ${failures} failed; finding kinds seen: ${[...rules].sort().join(", ")} (${rules.size}); runtime-text stderr differences: ${stderrDiffs}`);
process.exit(failures === 0 ? 0 : 1);
