// Entry for the black-box contract harness (the harness itself is harness/main.ts).
// Help is answered here, before ./main.ts (and its ./lib.ts, which creates a temp dir) is imported.
const USAGE = `usage: bun run harness [--image <tag>] [--subject-db fresh|golden]
                       [--phases routes,ingest,control,feeds,spa] [--routes <prefix,...>]

Black-box contract harness for the Bench Dashboard image: runs the subject (--image) beside a
reference twin on identical fixtures on a throwaway docker network. Exit 0 iff 0 checks failed.
  --image <tag>        image under test (default inference-dashboard:dev)
  --subject-db <mode>  fresh (default) or golden
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
