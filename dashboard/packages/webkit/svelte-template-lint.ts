// Owned Svelte template lint (svelte/compiler AST). Covers the template rules oxlint cannot
// see; anything the Svelte compiler itself reports is left to `svelte-check --fail-on-warnings`.
//   bun scripts/svelte-template-lint.ts [dir...]   (default: apps/web/src)
// Suppress with the fleet syntax on the line before the finding (reason required):
//   <!-- eslint-disable-next-line svelte/<rule> -- <reason> -->
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { parse } from "svelte/compiler";

export type Finding = { file: string; line: number; col: number; rule: string; message: string };

const RULES: Record<string, { type: string; test?: (n: AstNode) => boolean; message: string }> = {
  "no-at-html-tags": { type: "HtmlTag", message: "{@html} is an XSS vector; suppress with a reason if the input is trusted" },
  "no-at-debug-tags": { type: "DebugTag", message: "{@debug} must not be committed" },
  "require-each-key": { type: "EachBlock", test: (n) => !n.key, message: "{#each} block needs a (key)" },
};
const SUPPRESS = /^\s*eslint-disable-next-line\s+([^]*?)\s*(?:--\s*([^]*?))?\s*$/;

type AstNode = { type?: string; start: number; key?: unknown; data?: string; [k: string]: unknown };

function position(src: string, offset: number) {
  const before = src.slice(0, offset).split("\n");
  return { line: before.length, col: before[before.length - 1]!.length + 1 };
}

export function lintSource(src: string, file: string): Finding[] {
  let ast: ReturnType<typeof parse>;
  try {
    ast = parse(src, { modern: true });
  } catch (e) {
    const err = e as { start?: { line: number; column: number }; message?: string };
    const at = err.start ? { line: err.start.line, col: err.start.column + 1 } : { line: 1, col: 1 };
    return [{ file, ...at, rule: "parse-error", message: String(err.message ?? e).split("\n")[0]! }];
  }
  const raw: Finding[] = [];
  const suppressed = new Map<number, Set<string>>(); // line -> rules
  const visit = (node: unknown): void => {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) return node.forEach(visit);
    const n = node as AstNode;
    if (n.type === "Comment" && typeof n.data === "string") {
      const m = SUPPRESS.exec(n.data);
      if (m) {
        const at = position(src, n.start);
        const end = position(src, n.end as number);
        if (!m[2]) raw.push({ file, ...at, rule: "suppression-needs-reason", message: "eslint-disable-next-line needs `-- <reason>`" });
        else {
          const rules = m[1]!.split(",").map((r) => r.trim().replace(/^svelte\//, ""));
          suppressed.set(end.line + 1, new Set([...(suppressed.get(end.line + 1) ?? []), ...rules]));
        }
      }
    }
    for (const [rule, def] of Object.entries(RULES)) {
      if (n.type === def.type && (!def.test || def.test(n))) raw.push({ file, ...position(src, n.start), rule, message: def.message });
    }
    for (const k in n) if (k !== "parent" && k !== "instance" && k !== "module" && k !== "css") visit(n[k]);
  };
  visit(ast.fragment);
  return raw
    .filter((f) => !suppressed.get(f.line)?.has(f.rule))
    .sort((a, b) => a.line - b.line || a.col - b.col);
}

function svelteFiles(path: string): string[] {
  if (!statSync(path).isDirectory()) return path.endsWith(".svelte") ? [path] : [];
  return readdirSync(path).flatMap((f) => (f === "node_modules" ? [] : svelteFiles(join(path, f))));
}

export function lintPaths(paths: string[]): Finding[] {
  return paths.flatMap(svelteFiles).flatMap((f) => lintSource(readFileSync(f, "utf8"), f));
}

if (import.meta.main) {
  const args = process.argv.slice(2);
  if (args.includes("--help") || args.includes("-h")) {
    console.log("usage: bun svelte-template-lint.ts [path ...]   (default: apps/web/src; lints .svelte templates, exit 1 on findings)");
    process.exit(0);
  }
  const findings = lintPaths(args.length ? args : ["apps/web/src"]);
  for (const f of findings) console.log(`${f.file}:${f.line}:${f.col} ${f.rule} ${f.message}`);
  if (findings.length) {
    console.error(`svelte-template-lint: ${findings.length} finding(s)`);
    process.exit(1);
  }
}
