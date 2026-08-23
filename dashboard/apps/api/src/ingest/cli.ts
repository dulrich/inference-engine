import { config } from "../config.ts";
import { runIngest } from "./run.ts";

// `bun run ingest` (root) -> `bun run --filter '@bench-dashboard/api' ingest` ->
// `bun run src/ingest/cli.ts`. Hand-rolled argv parsing (fleet convention — no CLI-parsing
// dependency for a two-flag script; matches model_scoreboard's `scripts/*.ts` shape).
//
//   bun run ingest                    # scan config.recordsDir, upsert every v2 record
//   bun run ingest -- --dir <path>    # scan a different records dir
//   bun run ingest -- --rebuild       # drop + replay every v2 record from scratch

interface CliOptions {
  dir: string;
  rebuild: boolean;
}

export function parseArgs(argv: readonly string[]): CliOptions {
  const options: CliOptions = { dir: config.recordsDir, rebuild: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--rebuild") options.rebuild = true;
    else if (arg === "--dir") {
      const value = argv[i + 1];
      if (value === undefined) throw new Error("--dir requires a value");
      options.dir = value;
      i += 1;
    } else if (arg?.startsWith("--dir=")) {
      options.dir = arg.slice("--dir=".length);
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  return options;
}

if (import.meta.main) {
  const options = parseArgs(process.argv.slice(2));
  const summary = await runIngest({
    recordsDir: options.dir,
    rebuild: options.rebuild,
    onRecord: (event) => {
      if (event.kind === "invalid") console.error(`[ingest] invalid: ${event.dir} (${event.reason})`);
    },
  });
  console.log(JSON.stringify(summary));
}
