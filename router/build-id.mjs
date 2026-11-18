// build-id.mjs — fleet build-id helper (canonical copy-in template).
// Self-contained copy-in template: node builtins only. The fleet help gate imports this
// file directly, so the checker and the JS/TS helper are the same code.
//
// Build id = last commit under the tool root, ignoring control-plane paths (the exclude
// pathspecs below, relative to the tool root), plus `-dirty` when a tracked non-excluded
// file is modified; `unknown` without git metadata or when no commit matches.
//
//   import { handleBuildFlags } from "./build-id.mjs";
//   if (argv.includes("--help")) { ...usage...; process.exit(0); }   // --help wins
//   const rc = handleBuildFlags(argv, { path: import.meta.filename, name: "tool", semver: "1.2.3" });
//   if (rc !== null) process.exit(rc);

import { spawnSync } from "node:child_process";
import { lstatSync, realpathSync, statSync } from "node:fs";
import { dirname, join } from "node:path";

/** Pathspecs every build-id git call uses, relative to the tool root (same literal text in every language). */
export const BUILD_ID_EXCLUDES = Object.freeze([".", ":(exclude)plans", ":(exclude)references", ":(exclude,glob)**/FEEDBACK.md", ":(exclude,glob)**/INITIATIVE.md"]);

function git(cwd, args) {
  const r = spawnSync("git", ["--no-optional-locks", ...args], { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
  return r.status === 0 ? r.stdout : null;
}

function hasAgents(dir) {
  try {
    lstatSync(join(dir, "AGENTS.md"));
    return true;
  } catch {
    return false;
  }
}

/** Tool-scoped build id of `path`: `<40-hex>[-dirty]`, or `unknown` without git metadata. */
export function buildId(path) {
  let dir;
  try {
    const real = realpathSync(path);
    dir = statSync(real).isDirectory() ? real : dirname(real);
  } catch {
    return "unknown";
  }
  const top = git(dir, ["rev-parse", "--show-toplevel"])?.trim();
  if (!top) return "unknown";
  let tool = top;
  for (let d = dir; d === top || d.startsWith(`${top}/`); d = dirname(d)) {
    if (hasAgents(d)) {
      tool = d;
      break;
    }
    if (d === top) break;
  }
  const sha = git(top, ["-C", tool, "log", "-1", "--format=%H", "--", ...BUILD_ID_EXCLUDES])?.trim();
  if (!sha) return "unknown";
  const st = git(top, ["-C", tool, "status", "--porcelain", "--untracked-files=no", "--", ...BUILD_ID_EXCLUDES]);
  return st && st.trim() ? `${sha}-dirty` : sha;
}

/** Answers a bare top-level `--build` / `--version` (help precedence is the caller's). Returns the exit code, or null when neither flag is present. */
export function handleBuildFlags(argv, { path, name, semver } = {}) {
  const b = argv.includes("--build");
  const v = argv.includes("--version");
  if (!b && !v) return null;
  const id = buildId(path);
  process.stdout.write(v && !b && semver ? `${name} ${semver} (${id})\n` : `${id}\n`);
  return 0;
}
