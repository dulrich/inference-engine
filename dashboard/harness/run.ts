// Entry for the black-box contract harness (the harness itself is harness/main.ts).
// Help is answered here, before ./main.ts (and its ./lib.ts, which creates a temp dir) is imported.
const USAGE = `usage: bun run harness [--image <tag>] [--phases routes,control,feeds,spa] [--routes <prefix,...>]

Black-box contract harness for the serving dashboard image: drives one instance of --image on
fixtures on a throwaway docker network. Exit 0 iff 0 checks failed.
  --image <tag>        image under test (default inference-dashboard:dev)
  --phases <list>      phases to run (default all)
  --routes <prefixes>  path-prefix subset
  -h, --help           print this usage and exit 0
`;

if (process.argv.slice(2).some((a) => a === "--help" || a === "-h")) {
  process.stdout.write(USAGE);
  process.exit(0);
}
await import("./main.ts");

export {};
