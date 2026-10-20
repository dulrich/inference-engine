// Black-box contract harness for the Bench Dashboard image (plans/bench-dashboard-rust-port.md WP-1).
// Usage: bun run harness [--image <tag>] [--subject-db fresh|golden]
//                        [--phases routes,ingest,control,feeds,spa] [--routes <prefix,...>]
// Every check runs the subject (--image) beside a reference twin (the same --image)
// on identical fixtures and compares status + content type + normalized body, drop-box files and
// normalized DB dumps; pinned facts guard against a twin pair that agrees on nonsense.
// fresh (default): each twin of the read pair ingests the fixture records through its own ingest
// CLI. golden: --image ingests once, both twins serve a copy of that /data tree. --routes:
// path-prefix subset (`<path>$` = exact), out-of-subset checks skipped.
// Exit 0 iff 0 checks failed and every requested phase ran a check. Every docker object carries a
// per-run `bd-harness-<random>` prefix, publishes no host port, and is removed on exit.
import { cp, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { composeSpec, seedOne, seedPair, writeFixtures } from "./fixtures.ts";
import { controlPhase, feedsPhase, ingestPhase } from "./phases-files.ts";
import { routesPhase, spaPhase } from "./phases-http.ts";
import { check, cleanup, counts, idlePhases, image, ingestCommand, logs, network, phaseSummary, phases, prefix, run, setPhase, startApp, startPair, stopApp, subjectDb, wants, work, type Instance, type Pair } from "./lib.ts";

const started: Instance[] = [];
try {
  console.log(`harness ${prefix}: image ${image}, subject-db ${subjectDb}, phases ${phases.join(",")}`);
  await run(["docker", "image", "inspect", image]);
  console.log(`harness: ingest seam ${(await ingestCommand(image)).join(" ")}`);
  await run(["docker", "network", "create", network]);
  const fx = await writeFixtures();

  let seededDir: string | undefined;
  if (subjectDb === "golden") {
    setPhase("seed");
    const builder = await startApp("builder", image, await composeSpec(fx)());
    await seedOne(builder);
    await stopApp(builder);
    seededDir = builder.dataDir;
  }
  let pair: Pair | undefined;
  if (wants("routes", "spa")) {
    setPhase("seed");
    if (subjectDb === "golden") {
      const dirs: [string, string] = [join(work, "data", "subject-golden"), join(work, "data", "reference-golden")];
      for (const d of dirs) {
        await mkdir(d, { recursive: true });
        await cp(seededDir!, d, { recursive: true });
      }
      pair = await startPair("main", composeSpec(fx), dirs);
      check("subject serves the --image-built /data tree", true);
    } else {
      pair = await startPair("main", composeSpec(fx));
      await seedPair(pair);
    }
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
  if (wants("ingest")) {
    setPhase("ingest");
    await ingestPhase(fx);
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
