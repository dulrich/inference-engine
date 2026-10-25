// Black-box contract harness for the Bench Dashboard image (plans/bench-dashboard-rust-port.md WP-1).
// Usage: bun run harness [--image <tag>] [--phases routes,control,feeds,spa] [--routes <prefix,...>]
// Every check runs the subject (--image) beside a reference twin (the same --image)
// on identical fixtures and compares status + content type + normalized body and drop-box files;
// pinned facts guard against a twin pair that agrees on nonsense. --routes: path-prefix subset
// (`<path>$` = exact), out-of-subset checks skipped.
// Exit 0 iff 0 checks failed and every requested phase ran a check. Every docker object carries a
// per-run `bd-harness-<random>` prefix, publishes no host port, and is removed on exit.
import { composeSpec, writeFixtures } from "./fixtures.ts";
import { controlPhase, feedsPhase } from "./phases-files.ts";
import { routesPhase, spaPhase } from "./phases-http.ts";
import { check, cleanup, counts, idlePhases, image, logs, network, phaseSummary, phases, prefix, run, setPhase, startPair, wants, type Instance, type Pair } from "./lib.ts";

const started: Instance[] = [];
try {
  console.log(`harness ${prefix}: image ${image}, phases ${phases.join(",")}`);
  await run(["docker", "image", "inspect", image]);
  await run(["docker", "network", "create", network]);
  const fx = await writeFixtures();

  let pair: Pair | undefined;
  if (wants("routes", "spa")) {
    setPhase("setup");
    pair = await startPair("main", composeSpec(fx));
    started.push(...pair);
  }
  if (wants("routes")) {
    setPhase("routes");
    await routesPhase(pair!, fx);
  }
  if (wants("spa")) {
    setPhase("spa");
    await spaPhase(pair!);
  }
  if (wants("control")) {
    setPhase("control");
    await controlPhase(fx);
  }
  if (wants("feeds")) {
    setPhase("feeds");
    await feedsPhase();
  }
} catch (error) {
  check("harness error", false, error instanceof Error ? (error.stack ?? error.message) : String(error));
} finally {
  if (counts.failed > 0 && started[0]) console.log((await logs(started[0])) || "(no container logs)");
  await cleanup();
}
console.log(`harness: per phase ${phaseSummary()}`);
const idle = idlePhases();
if (idle.length > 0) console.log(`harness: requested phase(s) ran no check: ${idle.join(",")}`);
console.log(`harness: ${counts.passed} passed, ${counts.failed} failed, ${counts.skipped} skipped`);
process.exit(counts.failed === 0 && idle.length === 0 ? 0 : 1);
